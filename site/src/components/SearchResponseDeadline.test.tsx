import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import { SearchView } from "./SearchView";

afterEach(() => vi.restoreAllMocks());

async function search(status: "complete" | "partial" | "timeout", rows: object[] = []) {
  const user = userEvent.setup();
  vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify({
    results: rows,
    meta: { api_version: "query-v2", execution_status: status,
      returned_unique_studies: rows.length, limit_per_source_branch: 100,
      approximate: status !== "complete", truncated: status !== "complete",
      sources: { isrctn: {status: "included", coverage: "full", returned_unique_studies: 0,
        approximate: status !== "complete", truncated: status !== "complete",
        reason: status === "complete" ? null : "budget_exhausted"} },
    },
  }), {status: 200, headers: {"Content-Type": "application/json"}}));
  render(<SearchView selectedAxes={{state: {uri: "https://universalevidence.com/vocab/states/Stunting", label: "Stunting"}}}
    regionText="" onAxisChange={() => {}} onRegionTextChange={() => {}} />);
  await user.click(screen.getByRole("button", {name: "Search"}));
}

it("shows timeout rather than an empty successful search when no source finishes", async () => {
  await search("timeout");
  expect(await screen.findByText(/Search timed out before any source completed/)).toBeVisible();
  expect(screen.getByText(/ISRCTN did not finish within the search time limit/)).toBeVisible();
  expect(screen.queryByText("No studies matched the selected filters.")).not.toBeInTheDocument();
});

it("does not claim absence of evidence when only some sources finish empty", async () => {
  await search("partial");
  expect(await screen.findByText(/The search is incomplete/)).toBeVisible();
  expect(screen.queryByText("No studies matched the selected filters.")).not.toBeInTheDocument();
});

it("retains the ordinary no-matches message for completed empty searches", async () => {
  await search("complete");
  expect(await screen.findByText("No studies matched the selected filters.")).toBeVisible();
  expect(screen.queryByText(/Search timed out/)).not.toBeInTheDocument();
});

it("keeps available studies visible beside the unfinished-source notice", async () => {
  await search("partial", [{source: "AEA", study_id: "AEA-READY", title: "Completed nutrition study"}]);
  expect(await screen.findByText(/ISRCTN did not finish/)).toBeVisible();
  expect(screen.getByText("Completed nutrition study")).toBeVisible();
});
