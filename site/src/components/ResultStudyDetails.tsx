import type { ResultRow } from './InterventionResults';


type ObjectValue = Record<string, unknown>;
const object = (v: unknown): ObjectValue => v && typeof v === 'object' && !Array.isArray(v) ? v as ObjectValue : {};
const text = (v: unknown): string => v === undefined || v === null || v === '' ? 'Unavailable' : typeof v === 'object' ? JSON.stringify(v) : String(v);
const list = (v: unknown): unknown[] => Array.isArray(v) ? v : [];
const scopeText = (note: string) => note.replace(/Source evidence:[\s\S]*$/, '').replace(/Taxonomy identity does not imply supported statistical interpretation\.?/, '').trim();
export type ResultCoverage = {title?: string; discovery_interventions?: string[]; study: string; source: string; status: string; reason?: string; coverage?: {locator: string; status: string; title?: string; reason?: string; source_analysis?: unknown}[]};

function orderedContrast(comparison: ObjectValue): string {
  const groups = list(comparison.ordered_groups);
  const operator = comparison.statistic === 'difference' ? ' minus ' : comparison.statistic === 'ratio' ? ' divided by ' : null;
  if (!operator || groups.length !== 2 || !groups.every(g => typeof g === 'string' && g.trim())) return 'Unresolved';
  // The backend has already ordered these operands. Preserve the source estimate
  // and ratio subtype; source array order must not invert or negate it again.
  return groups.join(operator);
}

function denoms(context: ObjectValue) {
  const groupId = object(context.group).id;
  const show = (raw: unknown) => list(raw).flatMap(d => list(object(d).counts).filter(c => object(c).groupId === groupId).map(c => `${text(object(c).value)} ${text(object(d).units)}`)).join('; ');
  const assessment = show(context.class_denoms), outcome = show(context.outcome_denoms);
  return [assessment && `Assessment: ${assessment}`, outcome && `Outcome: ${outcome}`].filter(Boolean).join(' · ') || 'Unavailable';
}

function uncertainty(raw: unknown) {
  const u = object(raw);
  const parts = [];
  if (u.spread !== undefined || u.dispersionValue !== undefined) parts.push(`${text(u.dispersionType).replaceAll('_', ' ')}: ${text(u.spread ?? u.dispersionValue)}`);
  if (u.ciLowerLimit !== undefined || u.ciUpperLimit !== undefined || u.lowerLimit !== undefined || u.upperLimit !== undefined)
    parts.push(`${text(u.ciPctValue ?? u.dispersionType)}${u.ciPctValue ? '% confidence interval' : ''}: ${text(u.ciLowerLimit ?? u.lowerLimit)} to ${text(u.ciUpperLimit ?? u.upperLimit)}; ${text(u.ciNumSides).replaceAll('_', ' ')}`);
  if (u.pValue !== undefined) parts.push(`Reported p: ${text(u.pValue)}`);
  return parts.join(' · ') || (Object.keys(u).length?JSON.stringify(u):'Unavailable');
}

export function ResultStudyDetails({rows, coverage, intervention}: {rows: ResultRow[]; coverage: ResultCoverage[]; intervention: string}) {
  const studies = new Set(rows.filter(r => r.discovery_interventions?.includes(intervention) || r.result_interventions?.includes(intervention) || list(object(r.finding_mapping).tested).includes(intervention) || list(object(r.summary_mapping).tested).includes(intervention)).map(r => r.study));
  for (const c of coverage) if (c.discovery_interventions?.includes(intervention) && ![...studies].some(s => s.endsWith('/'+c.study) || s === c.study)) studies.add(c.study);
  return <div className="result-study-details">{[...studies].map(study => {
    const studyRows = rows.filter(r => r.study === study);
    const groups = new Map<string, ResultRow[]>();
    for (const row of studyRows) {const key = row.indicator || row.indicator_label; groups.set(key, [...(groups.get(key) ?? []), row]);}
    const sourceCoverage = coverage.filter(c => study.endsWith('/' + c.study) || study === c.study);
    return <details key={study}>
      <summary>{studyRows[0]?.citations?.[0]?.citation || sourceCoverage[0]?.title || study.split('/').pop()} · {groups.size} reported {groups.size === 1 ? 'indicator' : 'indicators'}</summary>
      <p className="result-note">Descriptive arm measurements do not establish an intervention effect. Reported interactions are excluded from effect summaries and ranking.</p>
      {[...groups].map(([key, records]) => <details key={key}>
        <summary>{records[0].indicator_label} · {records.filter(r => r.comparison?.kind !== 'source_comparison').length} arm measurements</summary>
        {records[0].measure_definition && <p>{records[0].measure_definition}</p>}
        {records[0].scope_notes?.map(note => <p className="result-note" key={note}>{scopeText(note)}</p>)}
        {records.map((row, rowIndex) => {
          const conceptLabel = (uri: string) => row.preferred_labels?.[uri] || 'Label unavailable';
          const comparison = object(row.comparison), context = object(comparison.context);
          const comparative = ['source_comparison','between-groups','association'].includes(String(comparison.kind));
          const gap = comparative && comparison.semantics === 'unsupported';
          const attribution = object(comparison.attribution);
          const components = list(context.components).map(v => conceptLabel(String(v))).join(' + ');
          return <article className="result-measurement" key={`${row.measurement}-${rowIndex}`}>
            {row.measurement_conflict && <p className="result-gap">Conflicting measurement payload or study identity; interpretation is unresolved.</p>}
            <h5>{gap ? 'Unresolved reported comparison' : comparative ? comparison.estimand === 'interaction' ? 'Reported treatment-by-time interaction' : 'Reported comparison' : 'Descriptive arm measurement'}</h5>
            {Object.entries(object(comparison.numerical_validation)).filter(([, value]) => object(value).status === 'invalid').map(([field, value]) => <p className="result-gap" key={field}>Source numerical issue ({field.replaceAll('_', ' ')}): {text(object(value).reason)}</p>)}
            {gap && <p className="result-gap">{text(comparison.reason || 'Explicit orientation and supported source semantics are required.')}</p>}
            {Boolean(row.result_text) && <p className="result-qualified-text">{text(row.result_text)}</p>}
            {Boolean(row.indicator_review) && <p className="result-note">Indicator qualification: {text(row.indicator_review)}</p>}
            <dl>
              <dt>{comparative ? 'Source groups' : 'Arm'}</dt><dd>{comparative ? list(context.groups).map(g => text(object(g).title)).join(' / ') : text(object(context.group).title)}</dd>
              <dt>Mapped components</dt><dd>{components || 'Unavailable'}{context.mapping_status === 'unresolved' && ' · Intervention mapping unresolved'}</dd>
              <dt>Assessment / scope</dt><dd>{text(comparative ? comparison.time_scope ?? context.timeFrame ?? row.timing : context.class ?? row.timing)}{!comparative && <> · Reported timeframe: {text(row.timing)}</>}</dd>
              <dt>Statistic and source estimate</dt><dd>{text(context.statistic ?? comparison.quantity ?? row.measure)}: {text(row.value)} {row.unit}{gap && ' (unresolved interpretation)'}</dd>
              <dt>Reported uncertainty</dt><dd>{uncertainty(row.uncertainty)}</dd>
              <dt>Denominator</dt><dd>{denoms(context)}</dd>
              <dt>Population / category</dt><dd>{text(context.population ?? row.subgroup)} / {text(context.category)}</dd>
              {comparative && <><dt>Ordered contrast</dt><dd>{orderedContrast(comparison)}</dd><dt>Model context</dt><dd>{text(comparison.model_context ?? context.adjustment)}</dd></>}
            </dl>
            {comparative && comparison.attribution !== undefined && <p>Tested components: {list(attribution.tested).map(v => conceptLabel(String(v))).join(' + ') || 'None assigned'} · Comparator: {list(attribution.comparator).map(v => conceptLabel(String(v))).join(' + ') || 'None assigned'} · Background: {list(attribution.background).map(v => conceptLabel(String(v))).join(' + ') || 'None assigned'}{attribution.package === true && ' · Combined intervention'}</p>}
            {context.mapping_status === 'unresolved' && <p className="result-gap">{text(context.mapping_reason || 'Source intervention mapping is unresolved; no attribution inferred.')}</p>}
            {!comparative && object(context.group).description !== undefined && <details><summary>Arm description and dose</summary><p>{text(object(context.group).description)}</p></details>}
            {row.citations?.map(c => <a key={c.url} href={c.url} target="_blank" rel="noreferrer">View source ↗</a>)}
          </article>;
        })}
      </details>)}
      {sourceCoverage.flatMap(c => c.coverage ?? []).filter(c => c.status !== 'unsupported_summary').map(c => <p className="result-note" key={c.locator}>{c.title || c.locator}: {c.status.replaceAll('_', ' ')} · {c.reason}</p>)}
    </details>;
  })}</div>;
}
