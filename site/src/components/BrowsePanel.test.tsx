import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { BrowsePanel } from "./BrowsePanel";

const stateUri = "https://universalevidence.com/vocab/states/Malnutrition";
const interventionUri = "https://universalevidence.com/vocab/interventions/NutritionEducation";

afterEach(() => vi.restoreAllMocks());

describe("BrowsePanel concept details", () => {
  it("links only valid non-collection State rows and keeps row controls separate", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify([
      { uri: stateUri, label: "Malnutrition", children: [] },
      {
        uri: "https://universalevidence.com/vocab/states/NutritionCollection",
        label: "Nutrition collection",
        collection: true,
        children: [],
      },
      { uri: "https://example.com/vocab/states/Foreign", label: "Foreign", children: [] },
    ]), { status: 200, headers: { "Content-Type": "application/json" } }));

    render(
      <BrowsePanel
        axis="state"
        selectedTerms={[]}
        onAdd={vi.fn()}
        onRemove={vi.fn()}
        onClose={vi.fn()}
      />,
    );

    const link = await screen.findByRole("link", { name: "Open concept page for Malnutrition" });
    expect(link).toHaveAttribute("href", stateUri);
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
    expect(screen.getAllByRole("button", { name: "+ Add" })[0]).not.toBe(link);
    expect(screen.queryByRole("link", { name: /Nutrition collection/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /Foreign/ })).not.toBeInTheDocument();
    await waitFor(() => expect(globalThis.fetch).toHaveBeenCalled());
  });

  it("uses the exact Intervention URI for Intervention rows", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify([
      { uri: interventionUri, label: "Nutrition education", children: [] },
    ]), { status: 200, headers: { "Content-Type": "application/json" } }));

    render(
      <BrowsePanel
        axis="intervention"
        selectedTerms={[]}
        onAdd={vi.fn()}
        onRemove={vi.fn()}
        onClose={vi.fn()}
      />,
    );

    expect(await screen.findByRole("link", { name: "Open concept page for Nutrition education" }))
      .toHaveAttribute("href", interventionUri);
  });
});
