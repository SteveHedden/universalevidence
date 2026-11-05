# Indicator identifiers — November 2026

The 879 indicators now use `https://universalevidence.com/vocab/indicators/`
and belong to that SKOS concept scheme. Their type remains `ue:Indicator`;
`ue:measures` and the reciprocal State links retain their meaning.

Individual indicator identifiers serve RDF, including browser visits. They do
not render HTML concept pages. Existing states-namespace identifiers return 308
redirects. `/vocab/indicator-migrations` publishes the complete `dcterms:isReplacedBy`
mapping, also recorded in `api/resources/indicator-migrations.json`.
Indicators are omitted from Browse; old indicator directory URLs redirect to
Browse home. State and Intervention pages do not display indicator listings. Indicator identifiers are excluded from the HTML
sitemap; State and Intervention pages remain unchanged in identity.

Historical production Fuseki graphs and checksummed result assets are immutable
releases. They retain their original identifiers and checksums. Runtime adapters
normalize indicator identifiers after reading these releases, including compact
result rows and nested summary metadata; context checks are performed against the
original evidence. Fresh vocabulary files and CT.gov crosswalks use new identifiers.
The pinned release loader remains tied to its original image and manifest. Do not
run it to deploy this API migration or rewrite its checksummed assets in place.
Future data releases must use the migration mapping when producing canonical
indicator references, while retaining original provenance and source hashes.

Two pre-existing measuredBy targets are outside the 879 declared indicators:
`states:ScreenPositivePrevalence` and `states:EducationalStatus`. This migration
preserves them; it does not invent definitions or change their classifications.

Verification covers all 879 old redirects, all 879 new RDF resources, scheme/type
separation, redirects from all nine former directory pages, sitemap exclusions, state pages without indicator listings, and
historical result normalization without changing numerical values or State IDs.
