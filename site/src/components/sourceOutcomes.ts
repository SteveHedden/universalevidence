import type { Outcome, StudyResult } from "../api/client";
function sourceOutcomeKind(outcome: Outcome): "planned" | "reported" | "unspecified" {
  if (!outcome || typeof outcome !== 'object') return 'unspecified';
  const labels = ['type', 'outcome_status', 'outcomeStatus'].map(k => String(outcome[k] || '').toLowerCase().replaceAll('_', '-'));
  if (labels.some(v => ['reported-result', 'adverse-event', 'reported', 'synthesized'].includes(v))) return 'reported';
  if (labels.some(v => ['pre-specified-measure', 'pre-specified', 'planned', 'primary', 'secondary', 'other', 'primary-outcome', 'secondary-outcome'].includes(v))) return 'planned';
  return 'unspecified';
}
export function partitionSourceOutcomes(study: StudyResult) {
  const result = {planned: [...(study.planned_outcomes || [])], reported: [...(study.source_reported_outcomes || [])], unspecified: [...(study.source_unspecified_outcomes || [])]};
  for (const outcome of study.outcomes || []) result[sourceOutcomeKind(outcome)].push(outcome);
  for (const key of ['planned','reported','unspecified'] as const) result[key] = [...new Map(result[key].map(v => [JSON.stringify(v),v])).values()];
  return result;
}
