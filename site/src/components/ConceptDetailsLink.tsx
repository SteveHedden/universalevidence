import type { CanonicalConceptType } from "../canonicalConcept";
import { validatedCanonicalConceptUri } from "../canonicalConcept";

type ConceptDetailsLinkProps = {
  uri: unknown;
  label: string;
  expectedType: CanonicalConceptType;
  collection?: boolean;
  className?: string;
};

export function ConceptDetailsLink({
  uri,
  label,
  expectedType,
  collection = false,
  className = "",
}: ConceptDetailsLinkProps) {
  const canonicalUri = validatedCanonicalConceptUri(uri, expectedType, collection);
  if (!canonicalUri) return null;

  return (
    <a
      className={`concept-details-link${className ? ` ${className}` : ""}`}
      href={canonicalUri}
      target="_blank"
      rel="noopener noreferrer"
      aria-label={`Open concept page for ${label}`}
      title={`Open concept page for ${label}`}
    >
      <span aria-hidden="true">↗</span>
    </a>
  );
}
