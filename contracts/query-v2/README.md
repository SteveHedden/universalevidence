# Search API reference

Use `GET /query/v2` to search across the four supported registries.
For interactive examples and parameter definitions, see the [API documentation](https://api.universalevidence.com/docs).

## Search filters

Supply at least one of `state`, `condition`, `intervention`, `outcome`, or `region`.
Each value must be a full UE vocabulary concept URI. Repeat a parameter to select several concepts.

- `state` matches a concept appearing as either a condition or an outcome topic.
- `condition` and `outcome` restrict matching to the specified role.
- Broader concepts include their descendants.
- Values within a filter use OR by default. Set its corresponding logic parameter, such as `state_logic=and`, to require every selected value.
- Different filters combine with AND.

For example:

```http
GET /query/v2?state=https%3A%2F%2Funiversalevidence.com%2Fvocab%2Fstates%2FMalaria
```

Omit `region` to search anywhere. Geographic matching depends on verified study locations and source coverage; a location elsewhere in a country does not satisfy a selected subnational region.

## Reading results

Responses contain `results` and `meta`. Check the metadata before treating a response as complete:

- `returned_unique_studies`: the number of underlying studies after verified identity joins; unlinked source-qualified records remain separate. A study may appear in multiple presentation rows.
- `sources`: availability and coverage for each registry.
- `truncated` and `approximate`: whether limits or incomplete coverage affect the response.
- `execution_status`: `complete`, `partial`, or `timeout`.
- `completed_sources` and `timed_out_sources`: which sources finished and which exhausted their execution budget.

The default server response budget is 10 seconds. Completed results can be returned when another source times out. An empty incomplete response does not mean that no relevant research exists.

Search limits to 100 **unique studies per upstream source request/branch** before expanding presentation rows. A combined response can contain more than 100 studies. Study registrations from different registries may describe the same underlying study.

The hosted service is intended for interactive use. Avoid bursts and uncontrolled parallel requests. Rate limiting is enforced by the hosting infrastructure; limits may change.

## Machine-readable definitions

- [Request schema](request.schema.json)
- [Response schema](response.schema.json)
- [Source capabilities](source-capabilities.json)

Graph endpoints use a separate response format. The older `/query` endpoint also differs from `/query/v2`.

## Selected-State Graph projection

Graph v2 shares Search's bounded source selection. With canonical `state` filters,
only selected roots whose query branches actually returned a study can receive its
mapped interventions. A State branch searches condition OR outcome. OR unions
branch populations; AND intersects source-qualified study identities before
projection. A study returned for A does not automatically qualify for selected B,
even if A and B overlap in the taxonomy. Truncation means non-observation is not
proof of non-membership in an exhaustive population.

In this mode, a direct mapping to descendant B is preserved as metadata but does
not add a B evidence edge unless B was selected and the study qualified for that
branch. Taxonomy navigation nodes can remain with zero evidence counts. Multiple
mapped interventions produce separate edges; repeated rows, roles and mapping
paths count a study only once per edge. Overall and node totals count distinct underlying studies after verified identity joins,
retaining source-qualified identities for unlinked records, rather than summing edge weights.

CT.gov and ISRCTN retain their registry Search populations without an additional
Graph-only State crosswalk gate. AEA and WHO ICTRP retain their stored mapping-backed
selection and direct intervention hydration. Stored root membership comes from the
same `matchesCondition`/`matchesOutcome` predicates used by their selector; it is
not inferred from another branch or described as lexical search evidence.

Explicit `condition`, `outcome`, intervention and region filters still qualify the
population through the Search planner. For example, `state=A&condition=B` requires
an A match in either role and a B match in the condition role. Only A receives
edges. Without canonical `state`, existing direct-coordinate projection and strict
role-specific behavior are unchanged. Missing intervention mappings remain visible
in Search and are disclosed as omitted, unattributed studies in Graph metadata.

### Support provenance and reconciliation

Each evidence edge contains disjoint `support_counts`: `search_only`,
`mapping_only`, and `both`. They sum to its unique-study weight. Edge details carry
`support_provenance` for each study:

- `selected_root`: the displayed State coordinate.
- `query_matches`: actual root membership with `retrieval_basis` (`search`,
  `mapping`, or `reported_outcome`) and `retrieval_role` (`condition`, `outcome`, or null when unknown).
- `direct_state_mappings`: independently known original State URIs and roles,
  with `root_relationship` (`exact`, `descendant`, or null for unrelated mappings).
  A descendant remains its own URI; it is never represented as a direct root mapping.
- `category`: the study's single support category for this edge.

For no-canonical-State requests, the coordinate is directly mapped and the query
membership list may be empty. Detail metadata's `support_counts` describes the
entire edge population, independently of the current page. The inspector labels
these categories “Search match”, “Mapped state” and “Both” and explains descendant
support. No category establishes effectiveness or a verified diagnosis.

`meta.sound` means the response obeys this projection contract and, for details,
reconciles with the aggregate's support population. It does **not** assert direct
State attribution for lexical matches. `approximate`, `truncated`, source coverage,
and omission counts continue to describe source or presentation incompleteness;
lexical provenance alone does not set `approximate`.

Aggregate and detail membership digests include selected-mode provenance as well
as study identities. Changed mappings or support basis therefore invalidate an edge
manifest even when its study count is unchanged. `projectionVersion` versions the
semantics separately from `schemaVersion: graph-v2`; cache identities, cursor
contexts and signed tokens include that version. Tokens from older projection
semantics are rejected. Source budgets and per-source post-union caps are unchanged.


## Verified study enrichment (projection version 3)

The existing `GET /query` and `GET /graph` remain unchanged. The explicit decision
for the graph-v2 projection is to merge only verified same-study registry groups.
CT.gov A and WHO B linked to S count overall 1, each contributing registry 1,
edge weight 1 and detail item 1; source counts overlap and are not additive.
Reports/findings are never additional studies or registries. There is no global
cap shared across registry adapters. Enrichment has a separate configurable
100-study candidate cap, before context filters, with deterministic ordering and
explicit truncation/partial metadata.

Eligible reported outcomes can independently retrieve S, absent a live registry
match. `match_provenance` labels this `reported_outcome`; it is not a fabricated
registry query match. Every requested role/filter/Boolean operator must qualify.
Graph additionally needs a verified mapped intervention. `meta.enrichment` reports
coverage and `enrichment_version` identifies the snapshot (including cap settings).
API rows add `study_identity`, `registry_records`, `planned_outcomes`,
`reported_findings` with citations/locators, and `finding_history`. The source
metadata retains each actual contributing registry. Existing graph support
categories remain disjoint per underlying study; reported-outcome support belongs
to mapping_only unless combined with independent search support. Its distinct
query_matches basis remains visible.

Retracted/superseded sources/findings cannot qualify outcome retrieval; valid
independent findings and registry-search support survive. History is accessible
through `GET /studies/enrichment?study=STUDY_URI` even after loss of search
eligibility. Cache identities, edge tokens and cursors include enrichment version;
a changed snapshot requires refreshing the graph. See
[the extraction, standards and migration contract](../../ontology/study-enrichment.md).

Source outcomes retain their extractor type: `pre-specified-measure` is planned;
`reported-result` and `adverse-event` appear in `source_reported_outcomes`, separate
from verified `reported_findings`. Unknown legacy entries appear in
`source_unspecified_outcomes`. These source arrays do not confer verified outcome
retrieval. Enriched rows expose read-before-edit revision `preconditions` for the
private writer; see `ontology/study-enrichment.md` for update/review semantics.
