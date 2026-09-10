import { render, screen } from "@testing-library/react";
import { createElement } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { App, buildEvidenceGraphPath } from "./App";

afterEach(() => {
  vi.restoreAllMocks();
});

describe("landing-page tutorial entry point", () => {
  it("links to the tutorial without starting the unused stats query", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response("{}", {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );

    render(createElement(App));

    expect(
      screen.getByRole("link", { name: /how to use universal evidence/i }),
    ).toHaveAttribute("href", "/how-to-use-universal-evidence.html");
    await screen.findByLabelText("API connected");
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(String(fetchMock.mock.calls[0]?.[0])).toMatch(/\/openapi\.json$/);
  });
});

describe("buildEvidenceGraphPath", () => {
  it("carries a unified State handoff on the canonical wire", () => {
    const handoff = buildEvidenceGraphPath({
      chips: {
        state: [{ uri: "https://universalevidence.com/vocab/states/Malaria", label: "Malaria" }],
      },
      logic: { state: "or" },
      regionText: "",
    });

    const url = new URL(handoff.path!, "https://universalevidence.com");
    expect(url.searchParams.getAll("state")).toEqual([
      "https://universalevidence.com/vocab/states/Malaria",
    ]);
    expect(url.searchParams.get("state_label")).toBe("Malaria");
    expect(url.searchParams.get("state_logic")).toBe("or");
    expect(url.searchParams.has("condition")).toBe(false);
  });

  it("carries selected States, interventions, and canonical Regions", () => {
    const handoff = buildEvidenceGraphPath({
      chips: {
        state: [
          { uri: "https://universalevidence.com/vocab/states/Malaria", label: "Malaria" },
          { uri: "https://universalevidence.com/vocab/states/Dengue", label: "Dengue" },
        ],
        intervention: [{
          uri: "https://universalevidence.com/vocab/interventions/MeditationIntervention",
          label: "Meditation intervention",
        }],
        region: [{ uri: "https://sws.geonames.org/192950/", label: "Kenya" }],
      },
      logic: { state: "and", intervention: "or", region: "or" },
      regionText: "",
    });
    const url = new URL(handoff.path!, "https://universalevidence.com");

    expect(url.pathname).toBe("/labs/malnutrition-evidence-graph.html");
    expect(url.searchParams.getAll("state")).toEqual([
      "https://universalevidence.com/vocab/states/Malaria",
      "https://universalevidence.com/vocab/states/Dengue",
    ]);
    expect(url.searchParams.getAll("intervention")).toEqual([
      "https://universalevidence.com/vocab/interventions/MeditationIntervention",
    ]);
    expect(url.searchParams.getAll("state_label")).toEqual(["Malaria", "Dengue"]);
    expect(url.searchParams.get("intervention_label")).toBe("Meditation intervention");
    expect(url.searchParams.get("state_logic")).toBe("and");
    expect(url.searchParams.get("region")).toBe("https://sws.geonames.org/192950/");
    expect(url.searchParams.get("region_label")).toBe("Kenya");
  });

  it("rejects unsupported raw Region text instead of dropping it", () => {
    const handoff = buildEvidenceGraphPath({
      chips: {
        state: [{ uri: "https://universalevidence.com/vocab/states/Malaria", label: "Malaria" }],
      },
      logic: {},
      regionText: "Kenya typed but not selected",
    });

    expect(handoff.path).toBeNull();
    expect(handoff.rejection).toContain("Region");
  });
});
