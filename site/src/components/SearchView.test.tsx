import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { beforeEach, afterEach, describe, expect, it, vi } from "vitest";

import { SearchView } from "./SearchView";
import type { SelectedAxes, TaxonomyClass, TaxonomyTerm } from "../api/client";

const challengeTerm: TaxonomyTerm = {
  uri: "https://universalevidence.com/vocab/conditions/ChildMalnutrition",
  label: "Child Malnutrition",
};

const kenyaTerm: TaxonomyTerm = {
  uri: "https://sws.geonames.org/192950/",
  label: "Kenya",
};

const badakhshanTerm: TaxonomyTerm = {
  uri: "https://sws.geonames.org/1147745/",
  label: "Badakhshan",
};

const emptyV2Response = {
  results: [],
  meta: {
    api_version: "query-v2",
    returned_unique_studies: 0,
    limit_per_source_branch: 100,
    truncated: false,
    approximate: false,
    sources: {},
  },
};

beforeEach(() => {
  vi.stubEnv("VITE_API_BASE_URL", "http://localhost:8010");
});

afterEach(() => {
  vi.unstubAllEnvs();
  vi.restoreAllMocks();
});

function SearchHarness() {
  const [selectedAxes, setSelectedAxes] = useState<SelectedAxes>({});
  const [regionText, setRegionText] = useState("");

  function onAxisChange(axis: TaxonomyClass, term: TaxonomyTerm | null) {
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

  return (
    <>
      <output data-testid="region-text">{regionText}</output>
      <SearchView
        selectedAxes={selectedAxes}
        regionText={regionText}
        onAxisChange={onAxisChange}
        onRegionTextChange={setRegionText}
      />
    </>
  );
}

function makeFetchMock(v2Response: unknown = emptyV2Response, regionTerms = [kenyaTerm]) {
  return vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
    const url = String(input);
    const body = url.includes("/taxonomy/condition")
      ? [challengeTerm]
      : url.includes("/taxonomy/region")
        ? regionTerms
        : v2Response;
    return new Response(JSON.stringify(body), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  });
}

describe("SearchView", () => {
  it("Search button disabled until a chip is added", () => {
    render(<SearchHarness />);
    expect(screen.getByRole("button", { name: "Search" })).toBeDisabled();
  });

  it("labels the first selector State, browses the State vocabulary, and queries by state URI", async () => {
    const user = userEvent.setup();
    render(<SearchHarness />);

    const fetchMock = makeFetchMock();
    const searchButton = screen.getByRole("button", { name: "Search" });

    expect(screen.getByText("State", { selector: ".cq-axis-name" })).toBeVisible();

    await user.type(screen.getByRole("combobox", { name: "State" }), "mal");
    await user.click(await screen.findByRole("button", { name: /child malnutrition/i }));

    expect(searchButton).toBeEnabled();

    await user.click(searchButton);

    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        `http://localhost:8010/query/v2?state=${encodeURIComponent(challengeTerm.uri)}&state_logic=or`,
        expect.objectContaining({ method: "GET" }),
      ),
    );
  });

  it("sends state and selected region together in one v2 request", async () => {
    const user = userEvent.setup();
    const fetchMock = makeFetchMock();

    render(<SearchHarness />);

    const searchButton = screen.getByRole("button", { name: "Search" });

    await user.type(screen.getByRole("combobox", { name: "Region" }), "Kenya");
    expect(screen.getByTestId("region-text")).toHaveTextContent("Kenya");
    await user.click(await screen.findByRole("button", { name: "Kenya" }));
    expect(screen.getByTestId("region-text")).toBeEmptyDOMElement();

    await user.type(screen.getByRole("combobox", { name: "State" }), "mal");
    await user.click(await screen.findByRole("button", { name: /child malnutrition/i }));
    expect(searchButton).toBeEnabled();

    await user.click(searchButton);

    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        `http://localhost:8010/query/v2?state=${encodeURIComponent(challengeTerm.uri)}&state_logic=or&region=${encodeURIComponent(kenyaTerm.uri)}&region_logic=or`,
        expect.objectContaining({ method: "GET" }),
      ),
    );
  });

  it("clears hidden raw Region text when a canonical Region is selected or removed", async () => {
    const user = userEvent.setup();
    makeFetchMock();
    render(<SearchHarness />);

    const regionInput = screen.getByRole("combobox", { name: "Region" });
    await user.type(regionInput, "Kenya");
    await user.click(await screen.findByRole("button", { name: "Kenya" }));
    expect(screen.getByTestId("region-text")).toBeEmptyDOMElement();

    await user.type(regionInput, "stale raw value");
    expect(screen.getByTestId("region-text")).toHaveTextContent("stale raw value");
    await user.click(screen.getByRole("button", { name: "Remove Kenya" }));

    expect(screen.getByTestId("region-text")).toBeEmptyDOMElement();
    expect(regionInput).toHaveValue("");
  });

  it("discloses sources excluded from an administrative-level result", async () => {
    const user = userEvent.setup();
    makeFetchMock(
      {
        results: [],
        meta: {
          ...emptyV2Response.meta,
          sources: {
            aea: {
              status: "excluded",
              coverage: "country_only",
              returned_unique_studies: 0,
              truncated: false,
              approximate: false,
              reason: "unsupported_admin_level",
            },
            "who-ictrp": {
              status: "excluded",
              coverage: "country_only",
              returned_unique_studies: 0,
              truncated: false,
              approximate: false,
              reason: "unsupported_admin_level",
            },
          },
        },
      },
      [badakhshanTerm],
    );
    render(<SearchHarness />);

    await user.type(screen.getByRole("combobox", { name: "Region" }), "Badakhshan");
    await user.click(await screen.findByRole("button", { name: "Badakhshan" }));
    await user.click(screen.getByRole("button", { name: "Search" }));

    expect(
      await screen.findByText(/AEA, WHO ICTRP cannot verify this administrative level and were excluded/i),
    ).toBeInTheDocument();
  });
});
