// @vitest-environment node

import { readFileSync } from "node:fs";

import { JSDOM } from "jsdom";
import { describe, expect, it } from "vitest";

const pagePath = new URL("../public/how-to-use-universal-evidence.html", import.meta.url);
const pageSource = readFileSync(pagePath, "utf8");
const dom = new JSDOM(pageSource, {
  url: "https://universalevidence.com/how-to-use-universal-evidence.html",
});
const { document } = dom.window;

describe("standalone How to use Universal Evidence page", () => {
  it("is a direct static page that does not request an API", () => {
    expect(pageSource).toContain("<!doctype html>");
    expect(document.title).toBe("How to use Universal Evidence");
    expect(document.querySelectorAll("script")).toHaveLength(0);
    expect(pageSource).not.toMatch(/\bfetch\s*\(/);
    expect(pageSource).not.toContain("/api/");
  });

  it("uses one six-step Malnutrition example covering the required workflow", () => {
    const steps = Array.from(document.querySelectorAll(".guide-step"));
    expect(steps).toHaveLength(6);
    steps.forEach((step) => expect(step.textContent).toMatch(/Malnutrition/i));

    const copy = document.body.textContent?.replace(/\s+/g, " ") ?? "";
    expect(copy).toMatch(/State.*condition or outcome/i);
    expect(copy).toMatch(/Remove Malaria/i);
    expect(copy).toMatch(/canonical Malnutrition suggestion/i);
    expect(copy).toMatch(/Intervention.*ANYTHING/i);
    expect(copy).toMatch(/Match any.*Match all/i);
    expect(copy).toMatch(/results are grouped by Intervention/i);
    expect(document.querySelector(".outcome-pill")).toBeNull();
    expect(copy).not.toMatch(/\btaxonomy\b/i);
    expect(copy).toMatch(/State and Intervention thesauri/i);
    expect(copy).toMatch(/source registry/i);
    expect(copy).toMatch(/Browse is available on desktop and phone screens/i);
    expect(copy).toMatch(/close the thesaurus drawer after choosing a term/i);
    expect(copy).toMatch(/Graph requires at least one State or Intervention/i);
    expect(copy).toMatch(/unresolved typed Region text or Outcome chips/i);
    expect(copy).toMatch(/Blue nodes are States and green nodes are Interventions/i);
    expect(copy).toMatch(/linked-study count, source breakdown, and contributing study links/i);
    expect(copy).toMatch(/evidence connections, not broader or narrower thesaurus relationships/i);
    expect(copy).toMatch(/connected concepts are equivalent/i);
  });

  it("provides semantic landmarks, accessible visuals, and working Search links", () => {
    expect(document.querySelector("header")).not.toBeNull();
    expect(document.querySelector("main#tutorial")).not.toBeNull();
    expect(document.querySelector("footer")).not.toBeNull();
    expect(document.querySelector('nav[aria-label="Tutorial sections"]')).not.toBeNull();
    expect(document.querySelectorAll("h1")).toHaveLength(1);
    expect(document.querySelectorAll(".guide-step article[aria-labelledby]")).toHaveLength(6);

    const graph = document.querySelector('svg[role="img"]');
    expect(graph?.getAttribute("aria-labelledby")).toBe(
      "graph-demo-title graph-demo-description",
    );
    expect(graph?.querySelector("title")?.textContent).toMatch(/Malnutrition/);
    expect(graph?.querySelector("desc")?.textContent).toMatch(/blue Malnutrition State node/);

    const backLinks = Array.from(document.querySelectorAll('a[href="/"]'));
    expect(backLinks.some((link) => /Back to Search/i.test(link.textContent ?? ""))).toBe(true);
    expect(document.querySelector('a[href="/#search"]')?.textContent).toMatch(/try Malnutrition/i);
    expect(document.querySelector(".skip-link")?.getAttribute("href")).toBe("#tutorial");
  });
});
