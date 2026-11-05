import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { StudyFindings } from "./StudyFindings";
import type { ReportedFinding } from "../api/client";
const finding: ReportedFinding = {id:"f",status:"active",state_uri:"https://universalevidence.com/vocab/states/Stunting",measure:"Stunting prevalence difference",comparison:{reference:"Usual care"},timing:{reported:"12 months",reference_point:"randomization"},value:-1.2,unit:"percentage points",direction:"decrease",significance:"not statistically significant",uncertainty:{lower:-3.2,upper:0.8},citations:[{id:"r",citation:"Synthetic report",url:"https://example.org/report",locator:"Table 2",status:"active"}]};
describe("StudyFindings",()=>{
 it("separates plans, estimates, citations and historical support",()=>{
  const {container}=render(<StudyFindings study={{planned_outcomes:[{measure:"Height"}],reported_findings:[finding],finding_history:[{...finding,id:"old",status:"superseded"}],registry_records:[{source:"ctgov",record_id:"A",url:"https://example.org/A"},{source:"who-ictrp",record_id:"B",url:"https://example.org/B"}],match_provenance:[{basis:"reported_outcome"}]}}/>);
  expect(screen.getByText("Planned outcomes (1)")).toBeInTheDocument();
  expect(screen.getByText("Reported findings (1)")).toBeInTheDocument();
  expect(screen.getByText(/Superseded, retracted/)).toBeInTheDocument();
  expect(container.textContent).toContain("-1.2 percentage points");
  expect(container.textContent).toContain("not statistically significant");
  expect(container.querySelectorAll('a[href="https://example.org/report"]')).toHaveLength(2);
  expect(container.querySelectorAll("details").length).toBeGreaterThan(2);
 });
 it("keeps zero distinct from unavailable and rejects script links",()=>{
  const {container}=render(<StudyFindings study={{reported_findings:[{...finding,value:0,citations:[{...finding.citations[0],url:"javascript:alert(1)"}]}]}}/>);
  expect(container.textContent).toContain("Reported value: 0");
  expect(container.querySelector('a[href^="javascript:"]')).toBeNull();
 });
});

it.each([false,true])('keeps extractor-shaped posted results out of plans (enriched=%s)', enriched => {
 const outcomes=[{type:'pre-specified-measure',measure:'Planned height'},{type:'reported-result',measure:'Posted height',value:-1.2,unit:'cm'},{type:'adverse-event',measure:'Nausea',value:5},{measure:'Legacy outcome'}];
 const {container}=render(<StudyFindings study={{outcomes,...(enriched?{study_identity:'https://example.org/study',reported_findings:[finding],planned_outcomes:[outcomes[0]],source_reported_outcomes:outcomes.slice(1,3),source_unspecified_outcomes:outcomes.slice(3)}:{})}}/>);
 const plans=Array.from(container.querySelectorAll('details')).find(d=>d.querySelector('summary')?.textContent?.startsWith('Planned outcomes'))!;
 expect(plans.textContent).toContain('Planned outcomes (1)');
 expect(plans.textContent).toContain('Planned height');
 expect(plans.textContent).not.toMatch(/Posted height|Nausea|Legacy outcome/);
 expect(container.textContent).toContain('Registry-reported results — not yet verified (2)');
 expect(container.textContent).toContain('Posted height');
 expect(container.textContent).toContain('Nausea');
 expect(container.textContent).toContain('"value":-1.2');
 expect(container.textContent).toContain('Registry outcomes — status unspecified (1)');
 expect(container.textContent).toContain(`Reported findings (${enriched?1:0})`);
});
