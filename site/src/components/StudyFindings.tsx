import { partitionSourceOutcomes } from "./sourceOutcomes";
import { resolveApiUrl } from "../api/client";
import type { ReportedFinding, StudyResult } from "../api/client";

function display(value: unknown): string {
  return typeof value === "string" ? value : JSON.stringify(value);
}
function safeUrl(value: string): string | undefined {
  try { const url = new URL(value); return ["https:", "http:"].includes(url.protocol) ? value : undefined; }
  catch { return undefined; }
}
function Finding({ finding, historical = false }: { finding: ReportedFinding; historical?: boolean }) {
  return <details className="reported-finding">
    <summary>{finding.measure}{historical ? ` — historical (${finding.eligibility ?? finding.status})` : ""}</summary>
    <p>Outcome state: <a href={safeUrl(finding.state_uri)}>{finding.state_uri.split("/").pop()}</a></p>
    <p>Comparison: {display(finding.comparison)}<br />Timing: {display(finding.timing)}</p>
    {finding.subgroup && <p>Subgroup: {finding.subgroup}</p>}
    <p>Direction: {finding.direction ?? "Not reported"} (relative to the stated quantity and comparison)</p>
    {finding.value !== undefined && finding.value !== null && <p>Reported value: {finding.value} {finding.unit}</p>}
    {finding.result_text && <p>{finding.result_text}</p>}
    {finding.uncertainty !== undefined && <p>Uncertainty: {display(finding.uncertainty)}</p>}
    {finding.significance && <p>Reported significance: {finding.significance}</p>}
    <ul>{finding.citations.map((citation, index) => <li key={`${citation.id}-${index}`}>
      <a href={safeUrl(citation.url)} target="_blank" rel="noreferrer">{citation.citation}</a>
      {" — "}{citation.locator} ({citation.status})
    </li>)}</ul>
  </details>;
}
export function StudyFindings({ study }: { study: StudyResult }) {
  const { planned, reported, unspecified } = partitionSourceOutcomes(study);
  return <div className="study-findings">
    {study.study_identity && <p><a href={resolveApiUrl(`/studies/enrichment?study=${encodeURIComponent(study.study_identity)}`)} target="_blank" rel="noreferrer">Open study findings and history</a></p>}
    {!!study.registry_records?.length && <p>Registry records: {study.registry_records.map((r, i) => <span key={`${r.source}-${r.record_id}`}>
      {i ? " · " : ""}<a href={safeUrl(r.url)} target="_blank" rel="noreferrer">{r.source}: {r.record_id}</a>
    </span>)}</p>}
    {study.match_provenance?.some(m => m.basis === "reported_outcome") && <p>Matched through a verified reported outcome.</p>}
    <details><summary>Planned outcomes ({planned.length})</summary>
      {planned.length ? <ul>{planned.map((p, i) => <li key={i}>{typeof p === "string" ? p : p.measure ?? p.description ?? "Unspecified measure"}</li>)}</ul> : <p>Planned measures unavailable.</p>}
    </details>
    {!!reported.length && <details><summary>Registry-reported results — not yet verified ({reported.length})</summary>
      <p>Reported by the source registry; not Teacher-verified findings.</p>
      <ul>{reported.map((o,i) => <li key={i}>{display(o)}</li>)}</ul>
    </details>}
    {!!unspecified.length && <details><summary>Registry outcomes — status unspecified ({unspecified.length})</summary>
      <ul>{unspecified.map((o,i) => <li key={i}>{display(o)}</li>)}</ul>
    </details>}
    <h4>Reported findings ({study.reported_findings?.length ?? 0})</h4>
    {study.reported_findings?.length ? study.reported_findings.map(f => <Finding key={f.id} finding={f} />) : <p>No verified findings available here. This does not mean no effect.</p>}
    {!!study.finding_history?.length && <details><summary>Superseded, retracted or withdrawn support history ({study.finding_history.length})</summary>
      <p>Historical findings do not qualify this study for outcome retrieval.</p>
      {study.finding_history.map(f => <Finding key={f.id} finding={f} historical />)}
    </details>}
  </div>;
}
