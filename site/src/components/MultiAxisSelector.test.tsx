import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { MultiAxisSelector } from "./MultiAxisSelector";

const stateUri = "https://universalevidence.com/vocab/states/Malnutrition";
const interventionUri = "https://universalevidence.com/vocab/interventions/NutritionEducation";

function renderSelector(uri = stateUri) {
  const onRemove = vi.fn();
  render(
    <MultiAxisSelector
      axis="state"
      label="State"
      placeholder="ANYTHING"
      terms={[{ uri, label: "Malnutrition" }]}
      logic="or"
      onAdd={vi.fn()}
      onRemove={onRemove}
      onLogicChange={vi.fn()}
      onBrowseOpen={vi.fn()}
      browseActive={false}
    />,
  );
  return onRemove;
}

describe("MultiAxisSelector concept details", () => {
  it("keeps the hierarchy browser keyboard reachable", async () => {
    const user = userEvent.setup();
    const onBrowseOpen = vi.fn();
    render(
      <MultiAxisSelector
        axis="state"
        label="State"
        placeholder="ANYTHING"
        terms={[]}
        logic="or"
        onAdd={vi.fn()}
        onRemove={vi.fn()}
        onLogicChange={vi.fn()}
        onBrowseOpen={onBrowseOpen}
        browseActive={false}
      />,
    );

    const browseButton = screen.getByRole("button", { name: "Browse State" });
    expect(browseButton.tabIndex).toBe(0);
    browseButton.focus();
    await user.keyboard("{Enter}");
    expect(onBrowseOpen).toHaveBeenCalledOnce();
  });

  it("keeps concept navigation and removal as distinct controls", async () => {
    const user = userEvent.setup();
    const onRemove = renderSelector();

    const link = screen.getByRole("link", { name: "Open concept page for Malnutrition" });
    expect(link).toHaveAttribute("href", stateUri);
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", "noopener noreferrer");

    const remove = screen.getByRole("button", { name: "Remove Malnutrition" });
    expect(remove).not.toBe(link);
    await user.click(remove);
    expect(onRemove).toHaveBeenCalledWith(stateUri);
  });

  it("renders an invalid or foreign chip as a nonlinked label", () => {
    renderSelector("https://example.com/vocab/states/Malnutrition");
    expect(screen.getByText("Malnutrition")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /concept page/i })).not.toBeInTheDocument();
  });

  it("uses the exact selected Intervention URI", () => {
    render(
      <MultiAxisSelector
        axis="intervention"
        label="Intervention"
        placeholder="ANYTHING"
        terms={[{ uri: interventionUri, label: "Nutrition education" }]}
        logic="or"
        onAdd={vi.fn()}
        onRemove={vi.fn()}
        onLogicChange={vi.fn()}
        onBrowseOpen={vi.fn()}
        browseActive={false}
      />,
    );

    expect(screen.getByRole("link", { name: "Open concept page for Nutrition education" }))
      .toHaveAttribute("href", interventionUri);
  });
});
