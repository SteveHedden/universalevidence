import { useEffect, useState } from "react";

import { queryStudiesForState, queryStudiesV2 } from "../api/client";
import type {
  QueryParams,
  QueryV2Meta,
  QueryV2Response,
  QueryV2Values,
  SelectedAxes,
  StudyResult,
  TaxonomyClass,
  TaxonomyTerm,
} from "../api/client";
import { BrowsePanel } from "./BrowsePanel";
import { MultiAxisSelector } from "./MultiAxisSelector";
import { SearchResults } from "./SearchResults";

export type Chips = Partial<Record<TaxonomyClass, TaxonomyTerm[]>>;
export type Logic = Partial<Record<TaxonomyClass, "and" | "or">>;
export type GraphHandoffState = {
  chips: Chips;
  logic: Logic;
  regionText: string;
};

const SEARCH_AXES: Array<{ id: TaxonomyClass; label: string; placeholder: string }> = [
  { id: "state", label: "State", placeholder: "ANYTHING" },
  { id: "intervention", label: "Intervention", placeholder: "ANYTHING" },
  { id: "region", label: "Region", placeholder: "ANYWHERE" },
];

type SearchViewProps = {
  selectedAxes: SelectedAxes;
  regionText: string;
  onAxisChange: (axis: TaxonomyClass, term: TaxonomyTerm | null) => void;
  onRegionTextChange: (value: string) => void;
  onGraphHandoffChange?: (state: GraphHandoffState) => void;
};

function studyKey(s: StudyResult): string {
  const identity = s.study_id ?? s.url ?? `${s.title ?? ""}|${s.year ?? ""}`;
  return `${s.source ?? ""}\x00${identity}`;
}

// Execute query across multi-chip axes.
// Within each axis: OR → union of per-term results; AND → intersection.
// Across axes: always AND (study must satisfy every filled axis).
async function executeQuery(
  chips: Chips,
  logic: Logic,
  countryFallback: string,
): Promise<{ results: StudyResult[]; meta: QueryV2Meta | null }> {
  const axes = SEARCH_AXES.map((a) => a.id).filter((ax) => (chips[ax]?.length ?? 0) > 0);
  const hasCountry = !chips.region?.length && countryFallback.trim().length > 0;

  if (axes.length === 0 && !hasCountry) return { results: [], meta: null };

  // Canonical taxonomy selections use the source-aware backend planner in one
  // request. Raw country text remains a legacy fallback until the UI requires
  // users to select a canonical region suggestion.
  if (!hasCountry) {
    const values: QueryV2Values = {};
    for (const axis of axes) {
      values[axis] = chips[axis]!.map((term) => term.uri);
    }
    return queryStudiesV2(values, logic) as Promise<QueryV2Response>;
  }

  if (axes.length === 0) {
    return {
      results: await queryStudiesForState({ country: countryFallback.trim() }),
      meta: null,
    };
  }

  // Embed the country fallback into each per-axis call so it acts as an AND filter
  // without requiring a separate axis query pass.
  const countryParam: QueryParams = hasCountry ? { country: countryFallback.trim() } : {};

  const axisData = await Promise.all(
    axes.map(async (ax) => {
      const terms = chips[ax]!;
      const axLogic = logic[ax] ?? "or";
      const batches = await Promise.all(
        terms.map((t) => queryStudiesForState({ [ax]: t.uri, ...countryParam } as QueryParams)),
      );
      const all = batches.flat();

      let ids: Set<string>;
      if (axLogic === "or") {
        ids = new Set(all.map(studyKey));
      } else {
        // AND: study must appear in every term's result set
        const sets = batches.map((b) => new Set(b.map(studyKey)));
        ids = new Set(sets[0]);
        for (const s of sets.slice(1)) {
          for (const id of ids) {
            if (!s.has(id)) ids.delete(id);
          }
        }
      }
      return { ids, studies: all };
    }),
  );

  // Intersect across axes
  let finalIds = axisData[0].ids;
  for (const { ids } of axisData.slice(1)) {
    finalIds = new Set([...finalIds].filter((id) => ids.has(id)));
  }

  // Deduplicate by (study_id, condition_concept) so a study with multiple conditions
  // produces one row per condition — allowing it to appear in multiple condition groups.
  const seen = new Set<string>();
  const final: StudyResult[] = [];
  for (const { studies } of axisData) {
    for (const study of studies) {
      const sid = studyKey(study);
      if (!finalIds.has(sid)) continue;
      const rowKey = `${sid}\x00${study.condition_concept ?? ""}\x00${study.intervention_concept_uri ?? ""}`;
      if (!seen.has(rowKey)) {
        seen.add(rowKey);
        final.push(study);
      }
    }
  }
  return { results: final, meta: null };
}

const SOURCE_NAMES: Record<string, string> = {
  aea: "AEA",
  ctgov: "ClinicalTrials.gov",
  isrctn: "ISRCTN",
  "who-ictrp": "WHO ICTRP",
};

function coverageNotice(meta: QueryV2Meta | null): string | null {
  if (!meta) return null;
  const entries = Object.entries(meta.sources);
  const exhausted = entries
    .filter(([, source]) => source.reason === "budget_exhausted")
    .map(([source]) => SOURCE_NAMES[source] ?? source);
  const unsupported = entries
    .filter(([, source]) => source.reason === "unsupported_admin_level")
    .map(([source]) => SOURCE_NAMES[source] ?? source);
  const unavailable = entries
    .filter(([, source]) => source.status === "unavailable" || source.status === "error")
    .map(([source]) => SOURCE_NAMES[source] ?? source);
  const messages: string[] = [];
  if (meta.execution_status === "timeout") {
    messages.push(meta.response_timeout_stage === "serialization"
      ? "Search timed out while preparing the response. Please try Search again."
      : "Search timed out before any source completed. Please try Search again.");
  }
  if (exhausted.length > 0) {
    messages.push(`${exhausted.join(", ")} did not finish within the search time limit; try Search again to retry those sources.`);
  }
  if (unsupported.length > 0) {
    messages.push(`${unsupported.join(", ")} cannot verify this administrative level and were excluded.`);
  }
  if (unavailable.length > 0) {
    messages.push(`${unavailable.join(", ")} are temporarily unavailable.`);
  }
  if (messages.length === 0 && meta.approximate) {
    messages.push("This is a bounded, approximate result set and may not include every matching study.");
  }
  return messages.length > 0 ? messages.join(" ") : null;
}

export function SearchView({
  selectedAxes,
  regionText,
  onAxisChange,
  onRegionTextChange,
  onGraphHandoffChange,
}: SearchViewProps) {
  const [chips, setChips] = useState<Chips>({});
  const [logic, setLogic] = useState<Logic>({});
  const [browseAxis, setBrowseAxis] = useState<TaxonomyClass | null>(null);

  // Push page content left so the fixed panel never covers the Search button
  useEffect(() => {
    document.body.classList.toggle("browse-open", browseAxis !== null);
    return () => document.body.classList.remove("browse-open");
  }, [browseAxis]);
  const [results, setResults] = useState<StudyResult[] | null>(null);
  const [queryMeta, setQueryMeta] = useState<QueryV2Meta | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Seed chips from selectedAxes on mount (e.g. navigating from Graph tab)
  useEffect(() => {
    const initial: Chips = {};
    for (const ax of SEARCH_AXES.map((a) => a.id)) {
      const term = selectedAxes[ax];
      if (term) initial[ax] = [term];
    }
    if (Object.keys(initial).length > 0) setChips(initial);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    onGraphHandoffChange?.({ chips, logic, regionText });
  }, [chips, logic, onGraphHandoffChange, regionText]);

  function handleAdd(axis: TaxonomyClass, term: TaxonomyTerm) {
    if (axis === "region") onRegionTextChange("");
    const existing = chips[axis] ?? [];
    if (existing.some((existingTerm) => existingTerm.uri === term.uri)) return;
    if (existing.length === 0) onAxisChange(axis, term);
    setChips((prev) => ({
      ...prev,
      [axis]: [...(prev[axis] ?? []), term],
    }));
  }

  function handleRemove(axis: TaxonomyClass, uri: string) {
    if (axis === "region") onRegionTextChange("");
    const remaining = (chips[axis] ?? []).filter((term) => term.uri !== uri);
    onAxisChange(axis, remaining[0] ?? null);
    setChips((prev) => ({
      ...prev,
      [axis]: (prev[axis] ?? []).filter((term) => term.uri !== uri),
    }));
  }

  function handleLogicChange(axis: TaxonomyClass, val: "and" | "or") {
    setLogic((prev) => ({ ...prev, [axis]: val }));
  }

  function handleBrowseToggle(axis: TaxonomyClass) {
    setBrowseAxis((prev) => (prev === axis ? null : axis));
  }

  const hasSelection =
    SEARCH_AXES.some((a) => (chips[a.id]?.length ?? 0) > 0) ||
    regionText.trim().length > 0;

  async function handleSearch() {
    setLoading(true);
    setError(null);
    try {
      const response = await executeQuery(chips, logic, regionText);
      setResults(response.results);
      setQueryMeta(response.meta);
    } catch (caught) {
      setResults(null);
      setQueryMeta(null);
      setError(caught instanceof Error ? caught.message : "Unable to fetch studies.");
    } finally {
      setLoading(false);
    }
  }

  const hasState = (chips.state?.length ?? 0) > 0;
  const groupBy = !hasState ? "condition" : "intervention";
  const contextLabels =
    groupBy === "intervention"
      ? (chips.state ?? []).map((t) => t.label)
      : (chips.intervention ?? []).map((t) => t.label);
  const queryNotice = coverageNotice(queryMeta);
  const incompleteEmpty = results?.length === 0 && queryMeta != null && (
    queryMeta.execution_status === "timeout" || queryMeta.execution_status === "partial"
    || Object.values(queryMeta.sources).some((source) =>
      source.reason === "budget_exhausted" || source.status === "error" || source.status === "unavailable")
  );

  return (
    <>
      <div className="view-panel">
        <div className="query-builder">
          <div className="cq-sentence">
            <div className="cq-pair">
              <span className="cq-text">What evidence exists for addressing</span>
              <span className="cq-axis-name">State</span>
              <MultiAxisSelector
                axis="state"
                label="State"
                placeholder="ANYTHING"
                terms={chips.state ?? []}
                logic={logic.state ?? "or"}
                onAdd={(term) => handleAdd("state", term)}
                onRemove={(uri) => handleRemove("state", uri)}
                onLogicChange={(val) => handleLogicChange("state", val)}
                onBrowseOpen={() => handleBrowseToggle("state")}
                browseActive={browseAxis === "state"}
                hideLabel
              />
            </div>
            <div className="cq-pair">
              <span className="cq-text">with</span>
              <MultiAxisSelector
                axis="intervention"
                label="Intervention"
                placeholder="ANYTHING"
                terms={chips.intervention ?? []}
                logic={logic.intervention ?? "or"}
                onAdd={(term) => handleAdd("intervention", term)}
                onRemove={(uri) => handleRemove("intervention", uri)}
                onLogicChange={(val) => handleLogicChange("intervention", val)}
                onBrowseOpen={() => handleBrowseToggle("intervention")}
                browseActive={browseAxis === "intervention"}
                hideLabel
              />
            </div>
            <div className="cq-pair">
              <span className="cq-text">in</span>
              <MultiAxisSelector
                axis="region"
                label="Region"
                placeholder="ANYWHERE"
                terms={chips.region ?? []}
                logic={logic.region ?? "or"}
                onAdd={(term) => handleAdd("region", term)}
                onRemove={(uri) => handleRemove("region", uri)}
                onLogicChange={(val) => handleLogicChange("region", val)}
                onBrowseOpen={() => handleBrowseToggle("region")}
                browseActive={browseAxis === "region"}
                hideLabel
                onInputChange={onRegionTextChange}
                controlledInputValue={regionText}
              />
            </div>
            <span className="cq-text cq-question-mark">?</span>
            <button type="button" onClick={handleSearch} disabled={!hasSelection || loading}>
              {loading ? "Searching" : "Search"}
            </button>
          </div>
        </div>

        {error ? <div className="error-state">{error}</div> : null}
        {!loading && !error && queryNotice ? <div className="warning-state">{queryNotice}</div> : null}
        {loading ? (
          <p className="loading-state">
            Searching across sources… available results will appear when the search finishes or reaches its time limit.
          </p>
        ) : null}
        {!loading && !error && results === null ? (
          <p className="empty-state">Select filters above and search to find matching evidence.</p>
        ) : null}
        {!loading && incompleteEmpty && queryMeta?.execution_status !== "timeout" ? (
          <p className="empty-state">No matching studies were returned from the sources that finished. The search is incomplete; please try again.</p>
        ) : null}
        {!loading && results && !incompleteEmpty ? <SearchResults results={results} groupBy={groupBy} contextLabels={contextLabels} /> : null}
      </div>

      <BrowsePanel
        axis={browseAxis}
        selectedTerms={browseAxis ? (chips[browseAxis] ?? []) : []}
        onAdd={(term) => browseAxis && handleAdd(browseAxis, term)}
        onRemove={(uri) => browseAxis && handleRemove(browseAxis, uri)}
        onClose={() => setBrowseAxis(null)}
      />
    </>
  );
}
