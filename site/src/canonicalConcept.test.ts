import { describe, expect, it } from "vitest";

import { canonicalConceptType, validatedCanonicalConceptUri } from "./canonicalConcept";

const stateUri = "https://universalevidence.com/vocab/states/Malnutrition";
const interventionUri = "https://universalevidence.com/vocab/interventions/NutritionEducation";

describe("canonical concept URI validation", () => {
  it("accepts exact Universal Evidence State and Intervention term URIs", () => {
    expect(canonicalConceptType(stateUri)).toBe("state");
    expect(canonicalConceptType(interventionUri)).toBe("intervention");
    expect(validatedCanonicalConceptUri(stateUri, "state")).toBe(stateUri);
    expect(validatedCanonicalConceptUri(interventionUri, "intervention")).toBe(interventionUri);
  });

  it.each([
    null,
    "",
    ` ${stateUri}`,
    "http://universalevidence.com/vocab/states/Malnutrition",
    "https://example.com/vocab/states/Malnutrition",
    "https://universalevidence.com/vocab/regions/KE",
    "https://universalevidence.com/vocab/states/",
    "https://universalevidence.com/vocab/states/Malnutrition/child",
    `${stateUri}?view=1`,
    `${stateUri}#details`,
    "not a URI",
  ])("rejects an untrusted or non-term identifier: %s", (value) => {
    expect(canonicalConceptType(value)).toBeNull();
  });

  it("rejects a valid URI for the wrong concept type and any collection row", () => {
    expect(validatedCanonicalConceptUri(stateUri, "intervention")).toBeNull();
    expect(validatedCanonicalConceptUri(stateUri, "state", true)).toBeNull();
  });
});
