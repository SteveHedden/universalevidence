import { describe, it, expect } from 'vitest';
import { initialSearchAxes } from './searchHandoff';
describe('concept search handoff',()=>{
 it('keeps default search unchanged',()=>expect(initialSearchAxes('').state?.label).toBe('Malaria'));
 it('selects a state URI',()=>expect(initialSearchAxes('?state=https%3A%2F%2Funiversalevidence.com%2Fvocab%2Fstates%2FStunting&state_label=Childhood+stunting').state?.label).toBe('Childhood stunting'));
 it('selects intervention without adding Malaria',()=>expect(initialSearchAxes('?intervention=https://universalevidence.com/vocab/interventions/CashTransfer&intervention_label=Cash+transfer')).toEqual({intervention:{uri:'https://universalevidence.com/vocab/interventions/CashTransfer',label:'Cash transfer'}}));
 it('rejects foreign or malformed URIs',()=>expect(initialSearchAxes('?state=https://evil.test/x').state?.label).toBe('Malaria'));
});
