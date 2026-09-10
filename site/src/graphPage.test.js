// @vitest-environment node

import { readFileSync } from "node:fs";

import { JSDOM, VirtualConsole } from "jsdom";
import { describe, expect, it } from "vitest";

const pageSource = readFileSync(
  new URL("../public/labs/malnutrition-evidence-graph.html", import.meta.url),
  "utf8",
);

// jsdom deliberately does not navigate. Replace that one browser boundary with
// an observable assignment while executing the rest of the checked-in page.
const instrumentedPageSource = pageSource.replaceAll(
  "window.location.assign(next);",
  "window.__testNavigation = next.href;",
);

const stateUri = "https://universalevidence.com/vocab/states/Malaria";
const interventionUri = "https://universalevidence.com/vocab/interventions/NutritionEducation";
const regionUri = "https://sws.geonames.org/192950/";

function jsonResponse(payload) {
  return {
    ok: true,
    status: 200,
    json: async () => payload,
  };
}

function graphPayload() {
  return {
    nodes: [
      {
        id: stateUri,
        label: "Malaria",
        class: "State",
        selected: true,
        studyCount: 1,
      },
      {
        id: interventionUri,
        label: "Nutrition education",
        class: "Intervention",
        selected: false,
        studyCount: 2,
      },
    ],
    edges: [{
      id: "malaria-nutrition",
      // Graph v2's canonical evidence-edge orientation is Intervention -> State.
      source: interventionUri,
      target: stateUri,
      kind: "evidence",
      weight: 2,
      source_counts: { AEA: 2 },
      studies: [{ source: "AEA", study_id: "AEA-1", title: "Contributing study" }],
    }],
    metadata: { stateNodes: 1, interventionNodes: 1 },
    meta: {},
  };
}

function openGraphPage({
  regionLookup = async () => [{ uri: regionUri, label: "Kenya" }],
  stateLookup = async () => [{ uri: stateUri, label: "Malaria" }],
  payload = graphPayload(),
  graphLookup = async () => payload,
  clipboardWrite = async () => {},
  viewportWidth = 1024,
} = {}) {
  const requests = [];
  const clipboardWrites = [];
  const scriptErrors = [];
  const virtualConsole = new VirtualConsole();
  virtualConsole.on("jsdomError", (error) => scriptErrors.push(error));

  const dom = new JSDOM(instrumentedPageSource, {
    beforeParse(window) {
      Object.defineProperty(window, "innerWidth", {
        configurable: true,
        value: viewportWidth,
        writable: true,
      });
      window.fetch = async (input) => {
        const url = String(input);
        requests.push(url);
        if (url.startsWith("/taxonomy/region?")) {
          return jsonResponse(await regionLookup());
        }
        if (url.startsWith("/taxonomy/state?")) {
          return jsonResponse(await stateLookup());
        }
        if (url.startsWith("/taxonomy/intervention?")) {
          return jsonResponse([]);
        }
        if (url.startsWith("/graph/v2/edges/")) {
          return jsonResponse({ results: payload.edges?.[0]?.studies ?? [], meta: {} });
        }
        return jsonResponse(await graphLookup(url));
      };
      Object.defineProperty(window.navigator, "clipboard", {
        configurable: true,
        value: {
          writeText: async (value) => {
            clipboardWrites.push(value);
            return clipboardWrite(value);
          },
        },
      });
      window.SVGElement.prototype.getBBox = () => ({
        x: 0,
        y: 0,
        width: 100,
        height: 100,
      });
      // Auto-fit is presentation-only and schedules work after test teardown.
      window.requestAnimationFrame = () => 0;
    },
    pretendToBeVisual: true,
    runScripts: "dangerously",
    url: `http://localhost/labs/malnutrition-evidence-graph.html?state=${encodeURIComponent(stateUri)}&state_label=Malaria`,
    virtualConsole,
  });

  return { clipboardWrites, dom, requests, scriptErrors };
}

async function waitFor(predicate, timeoutMs = 1_000) {
  const deadline = Date.now() + timeoutMs;
  while (!predicate()) {
    if (Date.now() >= deadline) throw new Error("Timed out waiting for graph page state");
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
}

describe("standalone Graph query controls", () => {
  it("expands taxonomy and evidence in place, deduplicates, and keeps edge query provenance", async () => {
    const child = `${interventionUri}Child`;
    const neighbor = `${stateUri}Neighbor`;
    const expansion = {
      nodes: [
        { id: interventionUri, label: 'Nutrition education', class: 'Intervention', selected: true },
        { id: child, label: 'Child intervention', class: 'Intervention' },
        { id: neighbor, label: 'Neighbor state', class: 'State' },
      ],
      edges: [
        { id: 'taxonomy-child', source: interventionUri, target: child, kind: 'hierarchy', weight: 1 },
        { id: 'expanded-evidence', source: interventionUri, target: neighbor, kind: 'evidence', weight: 3 },
      ],
    };
    const { dom, requests, scriptErrors } = openGraphPage({
      graphLookup: async url => new URL(url, 'http://localhost').searchParams.has('intervention') ? expansion : graphPayload(),
    });
    try {
      const { document } = dom.window;
      await waitFor(() => document.querySelectorAll('.node-circle').length === 2);
      document.querySelector(`[data-node-id="${interventionUri}"]`).dispatchEvent(
        new dom.window.MouseEvent('dblclick', { bubbles: true, cancelable: true }),
      );
      await waitFor(() => document.querySelectorAll('.node-circle').length === 4);
      expect(document.querySelector(`[data-node-id="${stateUri}"]`)).not.toBeNull();
      expect(document.querySelector(`[data-node-id="${child}"]`)).not.toBeNull();
      expect(document.querySelectorAll('.edge-line').length).toBe(3);
      expect(document.querySelector('#expansion-status').textContent).toContain('added 2 concepts');
      const graphRequests = () => requests.filter(url => url.startsWith('/graph/v2?'));
      expect(graphRequests()).toHaveLength(2);
      document.querySelector(`[data-node-id="${interventionUri}"]`).dispatchEvent(
        new dom.window.MouseEvent('dblclick', { bubbles: true }),
      );
      expect(graphRequests()).toHaveLength(2);
      document.querySelector('[data-edge-id="expanded-evidence"]').dispatchEvent(
        new dom.window.KeyboardEvent('keydown', { key: 'Enter', bubbles: true }),
      );
      await waitFor(() => requests.some(url => url.includes('/edges/expanded-evidence/studies')));
      const detail = new URL(requests.find(url => url.includes('/edges/expanded-evidence/studies')), 'http://localhost');
      expect(detail.searchParams.get('intervention')).toBe(interventionUri);
      expect(detail.searchParams.has('state')).toBe(false);
      await waitFor(() => document.querySelector('#edge-inspector').getAttribute('aria-busy') === 'false');
      expect(scriptErrors).toEqual([]);
    } finally { dom.window.close(); }
  });

  it("retains the graph after an expansion failure and allows retry", async () => {
    let failed = false;
    const { dom, scriptErrors } = openGraphPage({ graphLookup: async url => {
      if (new URL(url, 'http://localhost').searchParams.has('intervention') && !failed) {
        failed = true;
        return { nodes: [], edges: [] };
      }
      return graphPayload();
    } });
    try {
      const { document } = dom.window;
      await waitFor(() => document.querySelectorAll('.node-circle').length === 2);
      const expand = () => document.querySelector(`[data-node-id="${interventionUri}"]`).dispatchEvent(new dom.window.MouseEvent('dblclick', { bubbles: true }));
      expand();
      await waitFor(() => document.querySelector('#expansion-status').textContent.includes('Could not expand'));
      expect(document.querySelectorAll('.node-circle').length).toBe(2);
      expand();
      await waitFor(() => document.querySelector('#expansion-status').textContent.includes('added 0'));
      expect(document.querySelectorAll('.node-circle').length).toBe(2);
      expect(scriptErrors).toEqual([]);
    } finally { dom.window.close(); }
  });

  it("opens persistent exact concept details on node click and copies the canonical URI", async () => {
    const { clipboardWrites, dom, scriptErrors } = openGraphPage({ viewportWidth: 761 });
    try {
      await waitFor(() => dom.window.document.querySelector(`[data-node-id="${stateUri}"]`));
      const { document } = dom.window;
      const node = document.querySelector(`[data-node-id="${stateUri}"]`);

      expect(node.getAttribute("role")).toBe("button");
      expect(node.getAttribute("tabindex")).toBe("0");
      expect(node.getAttribute("aria-label")).toMatch(/State Malaria.*linked studies.*Open concept details/i);
      node.dispatchEvent(new dom.window.MouseEvent("click", { bubbles: true }));

      const inspector = document.querySelector("#edge-inspector");
      expect(inspector.getAttribute("aria-label")).toBe("Selected concept details");
      expect(inspector.textContent).toContain("State");
      expect(inspector.textContent).toContain("Malaria");
      expect(inspector.textContent).toContain(stateUri);
      expect(inspector.querySelector(".concept-summary").textContent).toContain(
        "2 linked studies across 1 Intervention.",
      );
      const openLink = inspector.querySelector(".concept-page-link");
      expect(openLink.getAttribute("href")).toBe(stateUri);
      expect(openLink.getAttribute("target")).toBe("_blank");
      expect(openLink.getAttribute("rel")).toBe("noopener noreferrer");

      inspector.querySelector("#copy-concept-uri").click();
      await waitFor(() => inspector.querySelector("#copy-concept-status").textContent === "URI copied.");
      expect(clipboardWrites).toEqual([stateUri]);
      expect(inspector.querySelector("#copy-concept-status").textContent).toBe("URI copied.");
      expect(scriptErrors).toEqual([]);
    } finally {
      dom.window.close();
    }
  });

  it("activates State and Intervention nodes with Enter and Space", async () => {
    const { dom, scriptErrors } = openGraphPage();
    try {
      await waitFor(() => dom.window.document.querySelectorAll(".node-circle").length === 2);
      const { document } = dom.window;
      const stateNode = document.querySelector(`[data-node-id="${stateUri}"]`);
      const stateKey = new dom.window.KeyboardEvent("keydown", { bubbles: true, cancelable: true, key: "Enter" });
      stateNode.dispatchEvent(stateKey);
      expect(stateKey.defaultPrevented).toBe(true);
      expect(document.querySelector("#edge-inspector h2").textContent).toBe("Malaria");

      const interventionNode = document.querySelector(`[data-node-id="${interventionUri}"]`);
      const interventionKey = new dom.window.KeyboardEvent("keydown", { bubbles: true, cancelable: true, key: " " });
      interventionNode.dispatchEvent(interventionKey);
      expect(interventionKey.defaultPrevented).toBe(true);
      expect(document.querySelector("#edge-inspector h2").textContent).toBe("Nutrition education");
      expect(document.querySelector("#edge-inspector .eyebrow").textContent).toBe("Intervention");
      expect(document.querySelector("#edge-inspector .concept-summary").textContent).toContain(
        "2 linked studies across 1 State.",
      );
      expect(scriptErrors).toEqual([]);
    } finally {
      dom.window.close();
    }
  });

  it("keeps the legacy node-pin interaction at the existing mobile breakpoint", async () => {
    const { dom, requests, scriptErrors } = openGraphPage({ viewportWidth: 760 });
    try {
      await waitFor(() => dom.window.document.querySelector(`[data-node-id="${stateUri}"]`));
      const { document } = dom.window;
      const node = document.querySelector(`[data-node-id="${stateUri}"]`);
      const inspector = document.querySelector("#edge-inspector");
      const tooltip = document.querySelector("#tooltip");

      expect(node.getAttribute("role")).toBeNull();
      expect(node.getAttribute("tabindex")).toBeNull();
      expect(node.getAttribute("aria-label")).toBeNull();

      node.dispatchEvent(new dom.window.MouseEvent("click", { bubbles: true }));
      expect(tooltip.style.display).toBe("block");
      expect(tooltip.textContent).toContain("Malaria");
      expect(inspector.querySelector(".concept-page-link")).toBeNull();
      expect(inspector.textContent).toContain("Select a connection");
      expect(inspector.getAttribute("aria-label")).toBe("Selected evidence studies");

      const enter = new dom.window.KeyboardEvent("keydown", {
        bubbles: true,
        cancelable: true,
        key: "Enter",
      });
      node.dispatchEvent(enter);
      expect(enter.defaultPrevented).toBe(false);
      expect(inspector.querySelector(".concept-page-link")).toBeNull();

      node.dispatchEvent(new dom.window.MouseEvent("click", { bubbles: true }));
      expect(tooltip.style.display).toBe("none");

      const edge = document.querySelector(".edge-hit");
      edge.dispatchEvent(new dom.window.KeyboardEvent("keydown", {
        bubbles: true,
        cancelable: true,
        key: "Enter",
      }));
      await waitFor(() => requests.some((url) => url.startsWith("/graph/v2/edges/"))
        && inspector.getAttribute("aria-busy") === "false");
      expect(inspector.querySelectorAll(".edge-route a")).toHaveLength(0);
      expect(Array.from(inspector.querySelectorAll(".edge-route span"), (element) => element.textContent))
        .toEqual(["Nutrition education", "Malaria"]);
      expect(scriptErrors).toEqual([]);
    } finally {
      dom.window.close();
    }
  });

  it("links exact canonical endpoints in the evidence inspector without changing edge activation", async () => {
    const { dom, requests, scriptErrors } = openGraphPage();
    try {
      await waitFor(() => dom.window.document.querySelector(".edge-hit"));
      const edge = dom.window.document.querySelector(".edge-hit");
      const enter = new dom.window.KeyboardEvent("keydown", { bubbles: true, cancelable: true, key: "Enter" });
      edge.dispatchEvent(enter);

      expect(enter.defaultPrevented).toBe(true);
      const endpointLinks = Array.from(dom.window.document.querySelectorAll(".edge-route a"));
      expect(endpointLinks.map((link) => link.getAttribute("href"))).toEqual([interventionUri, stateUri]);
      endpointLinks.forEach((link) => {
        expect(link.getAttribute("target")).toBe("_blank");
        expect(link.getAttribute("rel")).toBe("noopener noreferrer");
      });
      expect(dom.window.document.querySelector("#edge-inspector").textContent).toContain("Contributing study");
      await waitFor(() => requests.some((url) => url.startsWith("/graph/v2/edges/"))
        && dom.window.document.querySelector("#edge-inspector").getAttribute("aria-busy") === "false");
      expect(scriptErrors).toEqual([]);
    } finally {
      dom.window.close();
    }
  });

  it("announces copy failure and never links or copies an untrusted node id", async () => {
    const invalidPayload = {
      nodes: [{ id: "javascript:alert(1)", label: "Untrusted", class: "State", studyCount: 0 }],
      edges: [],
      metadata: {},
      meta: {},
    };
    const { clipboardWrites, dom, scriptErrors } = openGraphPage({
      payload: invalidPayload,
      clipboardWrite: async () => { throw new Error("denied"); },
    });
    try {
      await waitFor(() => dom.window.document.querySelector(".node-circle"));
      dom.window.document.querySelector(".node-circle").dispatchEvent(
        new dom.window.KeyboardEvent("keydown", { bubbles: true, cancelable: true, key: "Enter" }),
      );
      const inspector = dom.window.document.querySelector("#edge-inspector");
      expect(inspector.textContent).toContain("A valid canonical URI is not available");
      expect(inspector.querySelector(".concept-page-link")).toBeNull();
      expect(inspector.querySelector("#copy-concept-uri")).toBeNull();
      expect(clipboardWrites).toEqual([]);
      expect(scriptErrors).toEqual([]);
    } finally {
      dom.window.close();
    }
  });

  it("announces clipboard failure while leaving the full URI selectable", async () => {
    const { dom, scriptErrors } = openGraphPage({
      clipboardWrite: async () => { throw new Error("denied"); },
    });
    try {
      await waitFor(() => dom.window.document.querySelector(`[data-node-id="${stateUri}"]`));
      dom.window.document.querySelector(`[data-node-id="${stateUri}"]`).dispatchEvent(
        new dom.window.MouseEvent("click", { bubbles: true }),
      );
      const inspector = dom.window.document.querySelector("#edge-inspector");
      inspector.querySelector("#copy-concept-uri").click();
      await waitFor(() => inspector.querySelector("#copy-concept-status").textContent.length > 0);
      expect(inspector.querySelector("#copy-concept-status").textContent).toContain("Select and copy it manually");
      expect(inspector.querySelector(".concept-uri").textContent).toBe(stateUri);
      expect(scriptErrors).toEqual([]);
    } finally {
      dom.window.close();
    }
  });

  it("uses streamlined copy and automatic graph presentation", async () => {
    const { dom, scriptErrors } = openGraphPage();
    try {
      await waitFor(() => dom.window.document.querySelectorAll("#legend .legend-row").length === 3);
      const { document } = dom.window;

      expect(document.querySelector('label[for="evidence-query"]').textContent).toBe(
        "Condition or intervention",
      );
      expect(document.querySelector('label[for="region-query"]').textContent).toBe("Region");
      const form = document.querySelector('#evidence-form');
      const toggle = document.querySelector('#search-toggle');
      expect(form.hidden).toBe(true);
      expect(document.querySelector('#search-summary-text').textContent).toBe('Malaria → Any intervention · Any region');
      expect(toggle.getAttribute('aria-expanded')).toBe('false');
      toggle.click();
      expect(form.hidden).toBe(false);
      expect(document.activeElement.id).toBe('evidence-query');
      toggle.click();
      expect(form.hidden).toBe(true);
      expect(document.querySelector(".controls")).toBeNull();
      expect(document.querySelector("#graph-description")).toBeNull();
      expect(document.querySelector("#footer-note")).toBeNull();
      expect(document.querySelector("#legend").textContent.replace(/\s+/g, " ").trim()).toBe(
        "State Intervention Connection",
      );
      expect(pageSource).toContain(".attr('stroke', palette.edge)");
      expect(pageSource).not.toContain("--edge-hierarchy");
      expect(pageSource).not.toContain("--edge-evidence");
      expect(pageSource).not.toContain("Live evidence across");
      expect(pageSource).not.toContain("Taxonomy structure");
      expect(pageSource).not.toContain("Evidence (width = study count)");
      expect(scriptErrors).toEqual([]);
    } finally {
      dom.window.close();
    }
  });

  it("labels leading interventions even when a state dominates connection volume", async () => {
    const payload = graphPayload();
    payload.nodes = [payload.nodes[0], ...Array.from({ length: 20 }, (_, i) => ({
      id: `${interventionUri}${i}`, label: `Intervention ${i}`, class: "Intervention",
    }))];
    payload.edges = payload.nodes.slice(1).map((node, i) => ({
      id: `edge-${i}`, source: node.id, target: stateUri, kind: "evidence", weight: 20 - i,
    }));
    const { dom, scriptErrors } = openGraphPage({ payload });
    try {
      await waitFor(() => dom.window.document.querySelectorAll(".node-label").length === 21);
      const labels = [...dom.window.document.querySelectorAll(".node-label")];
      const visible = () => labels.filter(el => el.style.display !== "none").map(el => el.textContent);
      expect(visible()).toEqual(["Malaria", ...Array.from({ length: 8 }, (_, i) => `Intervention ${i}`)]);
      const hiddenNode = dom.window.document.querySelector(`[data-node-id="${interventionUri}19"]`);
      hiddenNode.dispatchEvent(new dom.window.MouseEvent("mouseenter"));
      expect(visible()).toContain("Intervention 19");
      hiddenNode.dispatchEvent(new dom.window.MouseEvent("mouseleave"));
      expect(visible()).not.toContain("Intervention 19");
      expect(scriptErrors).toEqual([]);
    } finally {
      dom.window.close();
    }
  });

  it("selects the first canonical suggestion with Enter and serializes it", async () => {
    const { dom, requests, scriptErrors } = openGraphPage();
    try {
      await waitFor(() => dom.window.document.title.startsWith("Malaria evidence graph"));
      const input = dom.window.document.querySelector("#region-query");
      input.value = "Kenya";
      input.dispatchEvent(new dom.window.Event("input", { bubbles: true }));

      await waitFor(() => dom.window.document.querySelector("#region-option-0"));
      const enter = new dom.window.KeyboardEvent("keydown", {
        bubbles: true,
        cancelable: true,
        key: "Enter",
      });
      input.dispatchEvent(enter);

      expect(enter.defaultPrevented).toBe(true);
      expect(input.value).toBe("");
      expect(dom.window.document.querySelector("#region-chips").textContent).toContain("Kenya");

      dom.window.document.querySelector("#evidence-form").requestSubmit();
      const destination = new URL(dom.window.__testNavigation);
      expect(destination.searchParams.getAll("region")).toEqual([regionUri]);
      expect(destination.searchParams.getAll("region_label")).toEqual(["Kenya"]);
      expect(requests.some((url) => url.startsWith("/taxonomy/region?q=Kenya"))).toBe(true);
      expect(scriptErrors).toEqual([]);
    } finally {
      dom.window.close();
    }
  });

  it("blocks graph submission while Region text is not a selected canonical term", async () => {
    const { dom, scriptErrors } = openGraphPage();
    try {
      await waitFor(() => dom.window.document.title.startsWith("Malaria evidence graph"));
      const input = dom.window.document.querySelector("#region-query");
      input.value = "Kenya typed but not selected";
      input.dispatchEvent(new dom.window.Event("input", { bubbles: true }));

      dom.window.document.querySelector("#evidence-form").requestSubmit();

      expect(dom.window.__testNavigation).toBeUndefined();
      expect(dom.window.document.querySelector("#query-status").textContent).toContain(
        "Select a Region from the suggestions",
      );
      expect(dom.window.document.activeElement).toBe(input);
      expect(scriptErrors).toEqual([]);
    } finally {
      dom.window.close();
    }
  });

  it("blocks graph submission while State or Intervention text is unselected", async () => {
    const { dom, scriptErrors } = openGraphPage();
    try {
      await waitFor(() => dom.window.document.title.startsWith("Malaria evidence graph"));
      const input = dom.window.document.querySelector("#evidence-query");
      input.value = "Dengue typed but not selected";

      dom.window.document.querySelector("#evidence-form").requestSubmit();

      expect(dom.window.__testNavigation).toBeUndefined();
      expect(dom.window.document.querySelector("#query-status").textContent).toContain(
        "Select a condition or intervention from the suggestions",
      );
      expect(dom.window.document.activeElement).toBe(input);
      expect(scriptErrors).toEqual([]);
    } finally {
      dom.window.close();
    }
  });

  it("does not restore stale suggestions after the Region field is cleared", async () => {
    let resolveLookup;
    const lookup = new Promise((resolve) => {
      resolveLookup = resolve;
    });
    const { dom, requests, scriptErrors } = openGraphPage({
      regionLookup: () => lookup,
    });
    try {
      await waitFor(() => dom.window.document.title.startsWith("Malaria evidence graph"));
      const input = dom.window.document.querySelector("#region-query");
      input.value = "Kenya";
      input.dispatchEvent(new dom.window.Event("input", { bubbles: true }));
      await waitFor(() => requests.some((url) => url.startsWith("/taxonomy/region?q=Kenya")));

      input.value = "";
      input.dispatchEvent(new dom.window.Event("input", { bubbles: true }));
      resolveLookup([{ uri: regionUri, label: "Kenya" }]);
      await new Promise((resolve) => setTimeout(resolve, 0));

      expect(dom.window.document.querySelectorAll("#region-results .query-result")).toHaveLength(0);
      expect(input.getAttribute("aria-expanded")).toBe("false");
      expect(scriptErrors).toEqual([]);
    } finally {
      dom.window.close();
    }
  });

  it("does not reopen Region suggestions when a pending lookup resolves after Escape", async () => {
    let resolveLookup;
    const lookup = new Promise((resolve) => {
      resolveLookup = resolve;
    });
    const { dom, requests, scriptErrors } = openGraphPage({
      regionLookup: () => lookup,
    });
    try {
      await waitFor(() => dom.window.document.title.startsWith("Malaria evidence graph"));
      const input = dom.window.document.querySelector("#region-query");
      input.value = "Kenya";
      input.dispatchEvent(new dom.window.Event("input", { bubbles: true }));
      await waitFor(() => requests.some((url) => url.startsWith("/taxonomy/region?q=Kenya")));

      input.dispatchEvent(new dom.window.KeyboardEvent("keydown", {
        bubbles: true,
        cancelable: true,
        key: "Escape",
      }));
      resolveLookup([{ uri: regionUri, label: "Kenya" }]);
      await new Promise((resolve) => setTimeout(resolve, 0));

      expect(input.value).toBe("Kenya");
      expect(dom.window.document.querySelectorAll("#region-results .query-result")).toHaveLength(0);
      expect(input.getAttribute("aria-expanded")).toBe("false");
      expect(scriptErrors).toEqual([]);
    } finally {
      dom.window.close();
    }
  });

  it("does not restore stale State suggestions after the unified field is cleared", async () => {
    let resolveLookup;
    const lookup = new Promise((resolve) => {
      resolveLookup = resolve;
    });
    const { dom, requests, scriptErrors } = openGraphPage({
      stateLookup: () => lookup,
    });
    try {
      await waitFor(() => dom.window.document.title.startsWith("Malaria evidence graph"));
      const input = dom.window.document.querySelector("#evidence-query");
      input.value = "Malaria";
      input.dispatchEvent(new dom.window.Event("input", { bubbles: true }));
      await waitFor(() => requests.some((url) => url.startsWith("/taxonomy/state?q=Malaria")));

      input.value = "";
      input.dispatchEvent(new dom.window.Event("input", { bubbles: true }));
      resolveLookup([{ uri: stateUri, label: "Malaria" }]);
      await new Promise((resolve) => setTimeout(resolve, 0));

      expect(dom.window.document.querySelectorAll("#evidence-results .query-result")).toHaveLength(0);
      expect(input.getAttribute("aria-expanded")).toBe("false");
      expect(scriptErrors).toEqual([]);
    } finally {
      dom.window.close();
    }
  });
});
