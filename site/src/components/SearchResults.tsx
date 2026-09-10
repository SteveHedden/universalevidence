import { useState } from "react";
import type { Outcome, StudyResult } from "../api/client";
import { ConceptDetailsLink } from "./ConceptDetailsLink";

type SearchResultsProps = {
  results: StudyResult[];
  groupBy?: "intervention" | "condition";
  contextLabels?: string[];
};

const STATUS_ORDER: Record<string, number> = {
  completed: 0,
  on_going: 1,
  in_development: 2,
};

const STATUS_LABEL: Record<string, string> = {
  completed: "Completed",
  on_going: "Ongoing",
  in_development: "In development",
};

const SOURCE_LABEL: Record<string, string> = {
  "CT.gov": "ClinicalTrials.gov",
};

function sortStudies(studies: StudyResult[]): StudyResult[] {
  return [...studies].sort((a, b) => {
    const sa = STATUS_ORDER[a.status ?? ""] ?? 3;
    const sb = STATUS_ORDER[b.status ?? ""] ?? 3;
    if (sa !== sb) return sa - sb;
    return Number(b.year ?? 0) - Number(a.year ?? 0);
  });
}

type InterventionGroup = {
  concept: string;
  conceptUri: string | null;
  studies: StudyResult[];
};

function studyIdentity(result: StudyResult, fallback = ""): string {
  const identity = result.study_id ?? result.url ?? result.title ?? fallback;
  return `${result.source ?? ""}\x00${identity}`;
}

function uniqueStudies(results: StudyResult[]): StudyResult[] {
  const studies = new Map<string, StudyResult>();
  results.forEach((result, index) => {
    const key = studyIdentity(result, `row-${index}`);
    if (!studies.has(key)) studies.set(key, result);
  });
  return Array.from(studies.values());
}

function groupResults(results: StudyResult[], groupBy: "intervention" | "condition"): {
  groups: InterventionGroup[];
  ungrouped: StudyResult[];
} {
  // Use source-qualified study identities within each group so the same study
  // never appears twice there (it may legitimately appear in multiple groups).
  const groupMap = new Map<string, { concept: string; conceptUri: string | null; studies: Map<string, StudyResult> }>();
  const ungroupedMap = new Map<string, StudyResult>();
  const mappedStudies = new Set<string>();

  results.forEach((result, index) => {
    const concept = groupBy === "condition" ? result.condition_concept : result.intervention_concept;
    if (concept?.trim()) {
      mappedStudies.add(studyIdentity(result, `row-${index}`));
    }
  });

  results.forEach((result, index) => {
    const concept = groupBy === "condition" ? result.condition_concept : result.intervention_concept;
    const conceptUri = groupBy === "condition" ? result.condition_concept_uri : result.intervention_concept_uri;
    const studyKey = studyIdentity(result, `row-${index}`);
    if (concept?.trim()) {
      const groupKey = conceptUri?.trim() || `label:${concept.trim().toLocaleLowerCase()}`;
      if (!groupMap.has(groupKey)) {
        groupMap.set(groupKey, { concept: concept.trim(), conceptUri: conceptUri ?? null, studies: new Map() });
      }
      groupMap.get(groupKey)!.studies.set(studyKey, result);
    } else if (!mappedStudies.has(studyKey)) {
      ungroupedMap.set(studyKey, result);
    }
  });

  const groups = Array.from(groupMap.values())
    .map(({ concept, conceptUri, studies }) => ({ concept, conceptUri, studies: Array.from(studies.values()) }))
    .sort((a, b) => b.studies.length - a.studies.length);

  return { groups, ungrouped: Array.from(ungroupedMap.values()) };
}

export function SearchResults({ results, groupBy = "intervention", contextLabels }: SearchResultsProps) {
  if (results.length === 0) {
    return <p className="empty-state">No studies matched the selected filters.</p>;
  }

  const { groups, ungrouped } = groupResults(results, groupBy);
  const distinctStudies = uniqueStudies(results);

  if (groups.length > 0) {
    const uniqueStudyCount = distinctStudies.length;
    const completedTotal = distinctStudies.filter((r) => r.status === "completed").length;
    return (
      <div className="study-list">
        <p className="results-summary">
          {uniqueStudyCount.toLocaleString()} {uniqueStudyCount === 1 ? "study" : "studies"}
          {" · "}
          {groups.length}{" "}
          {groupBy === "condition" ? `condition${groups.length === 1 ? "" : "s"}` : `intervention${groups.length === 1 ? "" : "s"}`}
          {contextLabels && contextLabels.length > 0 && (
            <span className="results-summary-context"> for {contextLabels.join(", ")}</span>
          )}
          {completedTotal > 0 && (
            <span className="results-summary-sub"> · {completedTotal} completed</span>
          )}
        </p>
        {groups.map((group) => (
          <InterventionGroupBlock
            key={`${group.conceptUri ?? "label"}:${group.concept}`}
            group={group}
            conceptType={groupBy === "condition" ? "state" : "intervention"}
          />
        ))}
        {ungrouped.length > 0 && (
          <OtherStudiesBlock studies={ungrouped} />
        )}
      </div>
    );
  }

  // Flat fallback when no requested grouping concepts matched. Presentation
  // expansion can produce several rows per study, so keep the fallback count
  // and cards study-level as well.
  const sorted = sortStudies(distinctStudies);
  const completedCount = sorted.filter((r) => r.status === "completed").length;
  return (
    <div className="study-list">
      <p className="results-summary">
        {sorted.length.toLocaleString()} {sorted.length === 1 ? "study" : "studies"} found
        {completedCount > 0 && (
          <span className="results-summary-sub"> · {completedCount} completed</span>
        )}
      </p>
      {sorted.map((result, index) => (
        <StudyCard key={`${result.source ?? ""}-${result.study_id ?? index}`} result={result} />
      ))}
    </div>
  );
}

// Only outcomes matched to a states.ttl concept are shown -- raw/unmatched
// outcome text is intentionally excluded from display.
function hasStateConcept(outcome: Outcome): outcome is Exclude<Outcome, string> & { state_concept: string } {
  return typeof outcome !== "string" && typeof outcome.state_concept === "string" && outcome.state_concept.trim().length > 0;
}

function matchedOutcomes(outcomes: Outcome[] | undefined): Array<Exclude<Outcome, string> & { state_concept: string }> {
  return (outcomes ?? []).filter(hasStateConcept);
}

function outcomeLabel(outcome: Outcome): string {
  return hasStateConcept(outcome) ? outcome.state_concept : "";
}

function aggregateOutcomeLabels(studies: StudyResult[]): Array<{ label: string; count: number }> {
  const counts = new Map<string, number>();
  const display = new Map<string, string>();
  for (const study of studies) {
    for (const outcome of matchedOutcomes(study.outcomes)) {
      const label = outcomeLabel(outcome).trim();
      const key = label.toLowerCase();
      if (key) {
        counts.set(key, (counts.get(key) ?? 0) + 1);
        if (!display.has(key)) display.set(key, label);
      }
    }
  }
  return Array.from(counts.entries())
    .sort((a, b) => b[1] - a[1])
    .map(([key, count]) => ({ label: display.get(key)!, count }));
}

function InterventionGroupBlock({
  group,
  conceptType,
}: {
  group: InterventionGroup;
  conceptType: "state" | "intervention";
}) {
  const [open, setOpen] = useState(false);
  const sorted = sortStudies(group.studies);
  const completedCount = sorted.filter((r) => r.status === "completed").length;
  const outcomeLabels = open ? aggregateOutcomeLabels(group.studies) : [];

  return (
    <div className="intervention-group" data-axis={conceptType}>
      <div className={`group-header${open ? " group-header-open" : ""}`}>
        <button
          type="button"
          className="group-toggle"
          onClick={() => setOpen((v) => !v)}
          aria-expanded={open}
        >
          <span className={`group-chevron${open ? " open" : ""}`}>›</span>
          <span className="group-label">{group.concept}</span>
          <span className="group-count">{group.studies.length} {group.studies.length === 1 ? "study" : "studies"}</span>
          {completedCount > 0 && (
            <span className="group-completed">{completedCount} completed</span>
          )}
        </button>
        <ConceptDetailsLink
          className="group-concept-link"
          uri={group.conceptUri}
          label={group.concept}
          expectedType={conceptType}
        />
      </div>
      {open && (
        <>
          {outcomeLabels.length > 0 && (
            <div className="group-outcomes">
              <span className="group-outcomes-label">Outcomes measured:</span>
              {outcomeLabels.slice(0, 8).map(({ label, count }) => (
                <span key={label} className="outcome-tag">
                  {label}{count > 1 && <span className="outcome-tag-count"> {count}</span>}
                </span>
              ))}
              {outcomeLabels.length > 8 && (
                <span className="outcome-tag outcome-tag-overflow">+{outcomeLabels.length - 8} more</span>
              )}
            </div>
          )}
          {sorted.map((result, i) => (
            <StudyCard key={studyIdentity(result, String(i))} result={result} />
          ))}
        </>
      )}
    </div>
  );
}

function OtherStudiesBlock({ studies }: { studies: StudyResult[] }) {
  const [open, setOpen] = useState(false);
  const sorted = sortStudies(studies);
  const completedCount = sorted.filter((r) => r.status === "completed").length;
  return (
    <div className="intervention-group">
      <button
        type="button"
        className={`group-header group-toggle${open ? " group-header-open" : ""}`}
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
      >
        <span className={`group-chevron${open ? " open" : ""}`}>›</span>
        <span className="group-label muted">Other studies</span>
        <span className="group-count">{studies.length} {studies.length === 1 ? "study" : "studies"}</span>
        {completedCount > 0 && (
          <span className="group-completed">{completedCount} completed</span>
        )}
      </button>
      {open && sorted.map((result, i) => (
        <StudyCard key={`other-${studyIdentity(result, String(i))}`} result={result} />
      ))}
    </div>
  );
}

function StudyCard({ result }: { result: StudyResult }) {
  const statusLabel = STATUS_LABEL[result.status ?? ""] ?? result.status;

  return (
    <div className="study-card">
      <div className="study-card-header">
        <div className="study-card-title">
          {result.url ? (
            <a href={result.url} target="_blank" rel="noreferrer">
              {result.title || result.study_id || "Untitled study"}
            </a>
          ) : (
            <span>{result.title || result.study_id || "Untitled study"}</span>
          )}
          {result.study_id && result.title ? (
            <span className="study-id">{result.study_id}</span>
          ) : null}
        </div>
        <div className="study-card-badges">
          {result.source ? (
            <span className="source-badge">{SOURCE_LABEL[result.source] ?? result.source}</span>
          ) : null}
          {result.year ? <span className="study-year">{result.year}</span> : null}
        </div>
      </div>

      {result.intervention && !result.intervention_concept ? (
        <p className="study-intervention">{result.intervention}</p>
      ) : null}

      <div className="study-card-footer">
        <div className="study-meta">
          {result.country ? <span>{result.country}</span> : null}
          {statusLabel ? (
            <span className={`study-status study-status-${result.status ?? "unknown"}`}>
              {statusLabel}
            </span>
          ) : null}
        </div>
        {renderOutcomes(result.outcomes)}
      </div>
    </div>
  );
}

function renderOutcomes(outcomes: Outcome[] | undefined) {
  const matched = matchedOutcomes(outcomes);
  if (matched.length === 0) return null;

  const shown = matched.slice(0, 4);
  const overflow = matched.length - shown.length;

  return (
    <div className="outcome-tags">
      {shown.map((outcome, index) => (
        <span key={index} className="outcome-tag" title={formatOutcomeFull(outcome)}>
          {formatOutcome(outcome)}
        </span>
      ))}
      {overflow > 0 && (
        <span className="outcome-tag outcome-tag-overflow">+{overflow} more</span>
      )}
    </div>
  );
}

function formatOutcome(outcome: Outcome): string {
  const text = outcomeLabel(outcome);
  return text.length > 55 ? text.slice(0, 53) + "…" : text;
}

function formatOutcomeFull(outcome: Outcome): string {
  if (typeof outcome === "string") return outcome;
  return [outcome.type, outcome.measure, outcome.description, outcome.time_frame]
    .filter(Boolean)
    .join(" | ");
}
