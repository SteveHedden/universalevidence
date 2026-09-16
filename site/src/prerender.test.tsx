import { StrictMode } from "react";
import { act, within } from "@testing-library/react";
import { hydrateRoot } from "react-dom/client";
import { expect, it, vi } from "vitest";
import { App } from "./App";
import { renderHome } from "./prerender";

it("hydrates the crawlable homepage without losing its content or controls", async () => {
  const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response("{}"));
  const container = document.createElement("div");
  container.innerHTML = renderHome();
  expect(fetchMock).not.toHaveBeenCalled();
  expect(container.textContent).toContain("Then follow the results to the original source.");
  document.body.append(container);
  const onRecoverableError = vi.fn();
  let root: ReturnType<typeof hydrateRoot>;
  try {
    await act(async () => {
      root = hydrateRoot(container, <StrictMode><App /></StrictMode>, { onRecoverableError });
    });
    expect(onRecoverableError).not.toHaveBeenCalled();
    expect(within(container.querySelector("#search") as HTMLElement).getByRole("button", { name: "Search", exact: true })).toBeEnabled();
  } finally {
    await act(async () => root?.unmount());
    container.remove();
    fetchMock.mockRestore();
  }
});
