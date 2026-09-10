import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AxisSelector } from "./AxisSelector";

const challengeTerm = {
  uri: "https://universalevidence.com/vocab/states/ChildMalnutrition",
  label: "Child Malnutrition",
  definition: "Child nutrition challenge",
};

afterEach(() => {
  vi.restoreAllMocks();
});

describe("AxisSelector", () => {
  it("calls the taxonomy endpoint as the user types and selects a URI", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(
      new Response(JSON.stringify([challengeTerm]), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );

    render(
      <AxisSelector
        axis="condition"
        label="Condition"
        placeholder="Child Malnutrition"
        onChange={onChange}
      />,
    );

    await user.type(screen.getByRole("combobox", { name: "Condition" }), "mal");

    await screen.findByText("Child Malnutrition");
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringMatching(/\/taxonomy\/condition\?q=mal&limit=10$/),
      expect.objectContaining({ method: "GET" }),
    );

    await user.click(screen.getByRole("button", { name: /child malnutrition/i }));
    expect(onChange).toHaveBeenLastCalledWith(challengeTerm);
  });

  it("renders loading, error, and empty states", async () => {
    const user = userEvent.setup();
    vi.spyOn(globalThis, "fetch").mockRejectedValueOnce(new Error("nope"));

    render(
      <AxisSelector
        axis="intervention"
        label="Intervention"
        placeholder="Nutrition Education"
        onChange={vi.fn()}
      />,
    );

    await user.type(screen.getByRole("combobox", { name: "Intervention" }), "nut");
    expect(screen.getByText("Loading terms")).toBeInTheDocument();
    await screen.findByText("Unable to load terms");
  });

  it("handles empty region results intentionally", async () => {
    const user = userEvent.setup();
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(
      new Response(JSON.stringify([]), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );

    render(
      <AxisSelector axis="region" label="Region" placeholder="Sub-Saharan Africa" onChange={vi.fn()} />,
    );

    await user.type(screen.getByRole("combobox", { name: "Region" }), "sub");

    await waitFor(() => expect(screen.getByText("No regions available yet")).toBeInTheDocument());
  });
});
