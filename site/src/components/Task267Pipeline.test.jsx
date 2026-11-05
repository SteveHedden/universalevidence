import {beforeAll, test, expect} from 'vitest';
import {execFileSync} from 'node:child_process';
import {resolve} from 'node:path';
import {render, screen, cleanup} from '@testing-library/react';
import {InterventionResults} from './InterventionResults';
import {summarizeFindings} from './resultFindings';
import {summarizeResults} from './resultSummaries';
const I='https://universalevidence.com/vocab/interventions/Example';
let cases;
beforeAll(()=>{
  // Generate from actual discovery deduplication and assembly, not hand-stamped flags.
  cases=JSON.parse(execFileSync(process.env.UE_TEST_PYTHON || 'python3',
    [resolve('../tests/task267_pipeline_fixture.py')],{encoding:'utf8',timeout:120000}));
},125000);
for(const name of ['versions','branches','same_hash']){
  test(`${name}: discovery -> assembly -> summary cannot restore a directional finding`,()=>{
    const reviewed=cases[name].reviewed_results;
    expect(reviewed.rows.map(r=>r.value).sort()).toEqual(['0.07','99']);
    // Also exercise the narrowed subset used by intervention views.
    for(const rows of [reviewed.rows,reviewed.rows.filter(r=>r.result_interventions?.includes(I))]){
      expect(rows.length).toBeGreaterThan(0);
      expect(summarizeResults(rows,I)).toEqual([]);
      expect(summarizeFindings(rows,I).every(f=>f.code==='uninterpretable')).toBe(true);
      render(<InterventionResults state="https://universalevidence.com/vocab/states/Stunting" intervention={I} context="{}" active resultsOnly reviewed={{...reviewed,rows}}>{null}</InterventionResults>);
      expect(screen.getAllByText('Unable to interpret').length).toBeGreaterThan(0);
      expect(screen.queryByText('Observed increase')).toBeNull();
      cleanup();
    }
  });
}
test('identical source payloads still support one finding',()=>{
  const rows=cases.identical.reviewed_results.rows;
  expect(rows).toHaveLength(1);
  expect(cases.identical.reviewed_results.partial).toBe(false);
  expect(summarizeFindings(rows,I)).toHaveLength(1);
  expect(summarizeFindings(rows,I)[0].code).toBe('increase');
});
