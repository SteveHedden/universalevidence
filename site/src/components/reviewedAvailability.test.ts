import { describe, expect, it } from 'vitest';
import { reviewedEvidenceAvailable as available } from './reviewedAvailability';
const stunting = 'https://universalevidence.com/vocab/states/Stunting';
describe('published reviewed-evidence availability', () => {
  it('allows exact Stunting with a single-state or absent state context', () => {
    expect(available(stunting, '{}')).toBe(true);
    expect(available(stunting, JSON.stringify({values:{state:[stunting]}}))).toBe(true);
  });
  it('rejects other states, mixed states, and malformed context', () => {
    expect(available('https://universalevidence.com/vocab/states/Malaria', '{}')).toBe(false);
    expect(available(stunting, JSON.stringify({values:{state:[stunting,'other']}}))).toBe(false);
    expect(available(stunting, 'invalid')).toBe(false);
    expect(available(stunting, JSON.stringify({values:{state:stunting}}))).toBe(false);
  });
});
