const STUNTING = 'https://universalevidence.com/vocab/states/Stunting';

export function reviewedEvidenceAvailable(state: string, context: string): boolean {
  if (state !== STUNTING) return false;
  try {
    const states = JSON.parse(context).values?.state;
    return !states || (Array.isArray(states) && states.length === 1 && states[0] === STUNTING);
  } catch { return false; }
}
