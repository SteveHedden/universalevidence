import { useEffect, useState } from "react";

import { getApiHealth } from "./api/client";
import type { ApiHealth, SelectedAxes, TaxonomyClass, TaxonomyTerm } from "./api/client";
import { SearchView } from "./components/SearchView";
import type { GraphHandoffState } from "./components/SearchView";

export function buildEvidenceGraphPath(
  handoff: GraphHandoffState,
): { path: string | null; rejection: string | null } {
  if (handoff.regionText.trim()) {
    return {
      path: null,
      rejection: "Choose a canonical Region suggestion before opening Graph; raw Region text is not supported.",
    };
  }
  if ((handoff.chips.outcome?.length ?? 0) > 0) {
    return {
      path: null,
      rejection: "Outcome is not a Graph axis. Remove the Outcome filter before opening Graph.",
    };
  }
  const params = new URLSearchParams();
  (["state", "intervention", "region"] as const).forEach((axis) => {
    const terms = handoff.chips[axis] ?? [];
    terms.forEach((term) => {
      params.append(axis, term.uri);
      params.append(`${axis}_label`, term.label);
    });
    if (terms.length) params.set(`${axis}_logic`, handoff.logic[axis] ?? "or");
  });
  if (!params.has("state") && !params.has("intervention")) {
    return {
      path: null,
      rejection: "Add at least one State or Intervention before opening Graph.",
    };
  }
  const query = params.toString();
  return {
    path: `/labs/malnutrition-evidence-graph.html?${query}`,
    rejection: null,
  };
}

export function App() {
  const [health, setHealth] = useState<ApiHealth>({ ok: false, status: "checking" });
  const [selectedAxes, setSelectedAxes] = useState<SelectedAxes>({
    state: { uri: "https://universalevidence.com/vocab/states/Malaria", label: "Malaria" },
  });
  const [regionText, setRegionText] = useState("");
  const [graphHandoff, setGraphHandoff] = useState<GraphHandoffState>({
    chips: {
      state: [{ uri: "https://universalevidence.com/vocab/states/Malaria", label: "Malaria" }],
    },
    logic: {},
    regionText: "",
  });

  useEffect(() => {
    let cancelled = false;
    getApiHealth().then((nextHealth) => {
      if (!cancelled) setHealth(nextHealth);
    });
    return () => {
      cancelled = true;
    };
  }, []);

  function updateAxis(axis: TaxonomyClass, term: TaxonomyTerm | null) {
    setSelectedAxes((current) => {
      const next = { ...current };
      if (term) {
        next[axis] = term;
      } else {
        delete next[axis];
      }
      return next;
    });
  }

  const evidenceGraphHandoff = buildEvidenceGraphPath(graphHandoff);

  return (
    <main className="app-shell">
      <header className="app-header">
        <div className="brand-lockup">
          <svg className="brand-mark" viewBox="44 16 112 112" aria-hidden="true" focusable="false">
            <defs>
              <clipPath id="logo-clip">
                <circle cx="100" cy="72" r="56" />
              </clipPath>
            </defs>
            <circle cx="100" cy="72" r="56" fill="#c9a547" />
            <g clipPath="url(#logo-clip)">
              <rect x="43" y="72"  width="114" height="6" fill="#f6f7f9" />
              <rect x="43" y="86"  width="114" height="6" fill="#f6f7f9" />
              <rect x="43" y="100" width="114" height="6" fill="#f6f7f9" />
              <rect x="43" y="114" width="114" height="6" fill="#f6f7f9" />
            </g>
          </svg>
          <div>
            <h1>Universal Evidence</h1>
            <p className="tagline">what was tried and what happened</p>
          </div>
        </div>
        <span
          className={health.ok ? "api-dot api-dot-ok" : "api-dot"}
          title={health.ok ? `API connected — ${health.title ?? health.status}` : `API unavailable — ${health.status}`}
          aria-label={health.ok ? "API connected" : "API unavailable"}
        />
      </header>

      <div className="stats-row">
        <div className="stats-summary">
          <span>Hundreds of thousands of studies accessible</span>
          <span aria-hidden>·</span>
          <span>4 registries</span>
        </div>
        <a className="tutorial-entry-link" href="/how-to-use-universal-evidence.html">
          How to use Universal Evidence
          <span aria-hidden="true">→</span>
        </a>
      </div>

      <div className="view-tabs-wrap">
      <nav className="view-tabs" aria-label="Explorer views">
        <button type="button" className="active">Search</button>
        {evidenceGraphHandoff.path ? (
          <a href={evidenceGraphHandoff.path}>Graph</a>
        ) : (
          <button type="button" disabled title={evidenceGraphHandoff.rejection ?? undefined}>Graph</button>
        )}
      </nav>
      {evidenceGraphHandoff.rejection ? (
        <p className="graph-handoff-rejection" role="status">{evidenceGraphHandoff.rejection}</p>
      ) : null}
      </div>

      <section className="workspace" id="search">
        <SearchView
          selectedAxes={selectedAxes}
          regionText={regionText}
          onAxisChange={updateAxis}
          onRegionTextChange={setRegionText}
          onGraphHandoffChange={setGraphHandoff}
        />
      </section>

    </main>
  );
}
