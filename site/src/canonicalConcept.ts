export type CanonicalConceptType = "state" | "intervention";

const CANONICAL_ORIGIN = "https://universalevidence.com";
const CONCEPT_PATH = /^\/vocab\/(states|interventions)\/[^/?#]+$/;

export function canonicalConceptType(uri: unknown): CanonicalConceptType | null {
  if (typeof uri !== "string" || uri.length === 0 || uri !== uri.trim()) return null;

  let parsed: URL;
  try {
    parsed = new URL(uri);
  } catch {
    return null;
  }

  const match = CONCEPT_PATH.exec(parsed.pathname);
  if (
    parsed.origin !== CANONICAL_ORIGIN
    || parsed.username
    || parsed.password
    || parsed.search
    || parsed.hash
    || parsed.href !== uri
    || !match
  ) {
    return null;
  }

  return match[1] === "states" ? "state" : "intervention";
}

export function validatedCanonicalConceptUri(
  uri: unknown,
  expectedType?: CanonicalConceptType,
  collection = false,
): string | null {
  if (collection) return null;
  const actualType = canonicalConceptType(uri);
  if (!actualType || (expectedType && actualType !== expectedType)) return null;
  return uri as string;
}
