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

- `returned_unique_studies`: the number of distinct source-qualified studies returned. A study may appear in multiple presentation rows.
- `sources`: availability and coverage for each registry.
- `truncated` and `approximate`: whether limits or incomplete coverage affect the response.
- `execution_status`: `complete`, `partial`, or `timeout`.
- `completed_sources` and `timed_out_sources`: which sources finished and which exhausted their execution budget.

The default server response budget is 10 seconds. Completed results can be returned when another source times out. An empty incomplete response does not mean that no relevant research exists.

Search limits each upstream source request/branch to 100 unique studies before expanding presentation rows. A combined response can contain more than 100 studies. Study registrations from different registries may describe the same underlying study.

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
paths count a study only once per edge. Overall and node totals count distinct
source-qualified studies, rather than summing edge weights.

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
- `query_matches`: actual root membership with `retrieval_basis` (`search` or
  `mapping`) and `retrieval_role` (`condition`, `outcome`, or null when unknown).
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
