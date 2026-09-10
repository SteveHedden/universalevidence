import { afterEach, describe, expect, it, vi } from "vitest";

import { apiGet, buildQueryV2Path, getApiHealth, readApiBaseUrl, resolveApiUrl } from "./client";

afterEach(() => {
  vi.restoreAllMocks();
});

describe("API client configuration", () => {
  it("trims and normalizes VITE_API_BASE_URL", () => {
    expect(readApiBaseUrl({ VITE_API_BASE_URL: " http://localhost:8000/ " })).toBe(
      "http://localhost:8000",
    );
  });

  it("falls back to same-origin paths when no base URL is configured", () => {
    expect(resolveApiUrl("query", "")).toBe("/query");
    expect(resolveApiUrl("/graph", "")).toBe("/graph");
  });

  it("joins configured base URLs and paths", () => {
    expect(resolveApiUrl("/query", "http://localhost:8000")).toBe(
      "http://localhost:8000/query",
    );
  });
});

describe("apiGet", () => {
  it("fetches JSON from the resolved URL", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(
      new Response(JSON.stringify({ ok: true }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );

    await expect(apiGet<{ ok: boolean }>("/query")).resolves.toEqual({ ok: true });
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining("/query"),
      expect.objectContaining({ method: "GET" }),
    );
  });
});

describe("query v2 request", () => {
  it("uses repeated axis parameters and explicit per-axis logic", () => {
    expect(
      buildQueryV2Path(
        {
          condition: ["https://example.org/Malaria", "https://example.org/Dengue"],
          region: ["https://sws.geonames.org/192950/", "https://sws.geonames.org/226074/"],
        },
        { condition: "or", region: "and" },
      ),
    ).toBe(
      "/query/v2?condition=https%3A%2F%2Fexample.org%2FMalaria&condition=https%3A%2F%2Fexample.org%2FDengue&condition_logic=or&region=https%3A%2F%2Fsws.geonames.org%2F192950%2F&region=https%3A%2F%2Fsws.geonames.org%2F226074%2F&region_logic=and",
    );
  });

  it("serializes the unified state axis independently from strict condition and outcome", () => {
    expect(
      buildQueryV2Path(
        { state: ["https://example.org/Stunting"] },
        { state: "and" },
      ),
    ).toBe(
      "/query/v2?state=https%3A%2F%2Fexample.org%2FStunting&state_logic=and",
    );
  });
});

describe("getApiHealth", () => {
  it("returns the FastAPI app title when openapi.json is reachable", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(
      new Response(JSON.stringify({ info: { title: "Universal Evidence API" } }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );

    await expect(getApiHealth()).resolves.toEqual({
      ok: true,
      status: 200,
      title: "Universal Evidence API",
    });
  });
});
