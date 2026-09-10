# Query v2 contract

Status: implemented

Contract version: `query-v2`

Frozen: 2026-08-11

This document defines the source-aware, hierarchical search contract used by the Search interface. The existing `GET /query`, `GET /graph`,
and their cache keys and response shapes remain unchanged. The Search frontend uses the v2 State semantics.

The machine-readable companions are:

- `contracts/query-v2/request.schema.json`
- `contracts/query-v2/response.schema.json`
- `contracts/query-v2/source-capabilities.json`
- `tests/fixtures/query_v2/source_adapter_cases.json`
- `tests/fixtures/query_v2/canary_queries.json`

## Why v2 exists

The former frontend issued separate capped searches for selected chips and
combined their study IDs in the browser. That made an intersection such as
Malaria in Chittagong incomplete and can make it geographically false: an ADM1
selection may be reduced to Bangladesh before two independently truncated
lists are intersected.

Applying a row cap after expanding studies into intervention rows can hide
relevant studies. V2 sends the complete logical query to one backend plan and
applies limits to unique studies inside each source branch before presentation
expansion.

## HTTP request

`GET /query/v2` accepts repeated query parameters:

- `state`
- `condition`
- `intervention`
- `outcome`
- `region`

Every supplied value is a full canonical taxonomy URI. Duplicate values are
removed during canonicalization without changing semantics. At least one axis
must contain a value.

Each axis has one optional logic parameter:

- `state_logic`
- `condition_logic`
- `intervention_logic`
- `outcome_logic`
- `region_logic`

The only accepted values are lowercase `or` and `and`; each defaults to `or`.
Invalid values return HTTP 422. Cross-axis logic is always AND:

```text
(states joined by state_logic)
AND (conditions joined by condition_logic)
AND (interventions joined by intervention_logic)
AND (outcomes joined by outcome_logic)
AND (regions joined by region_logic)
```

Each selected `state` means a study whose condition **or** outcome is mapped to
that State concept. The role union is evaluated first for each selected State;
`state_logic` then combines those per-State study sets using `or` or `and`.
Explicit `condition` and `outcome` remain strict role-specific axes, and all
cross-axis combinations remain intersections. The legacy `country` alias
remains v1-only; v2 clients use canonical region URIs.

Graph search is not part of this contract. `GET /graph` remains unchanged;
The graph-v2 projection uses the shared query-plan interfaces and has its own response contract.

Example:

```http
GET /query/v2?state=https%3A%2F%2Funiversalevidence.com%2Fvocab%2Fstates%2FMalaria&region=https%3A%2F%2Fsws.geonames.org%2F1337200%2F
```

## Geographic semantics

- **World:** omit the geographic predicate. Do not expand World into every
  country.
- **Regional group:** expand to its descendant countries and ADM1 regions.
- **Country:** require at least one verified study location in that country.
- **ADM1:** require at least one verified location in that ADM1. A location
  somewhere else in the parent country is not a match.
- **Region OR:** require a verified location in any selected region.
- **Region AND:** use study-level multisite semantics. The study must have a
  verified location in every selected region, but those locations may be
  different sites.

Expansion is hierarchical. Selecting a region includes verified locations in
its descendants. A requested predicate must never be skipped, broadened, or
replaced by its parent merely because a source cannot evaluate it.

## Source capability matrix

The JSON capability matrix is authoritative for identifiers and machine use.
The human-readable summary is:

| Source ID | Country | ADM1 | Required ADM1 evidence |
|---|---|---|---|
| `ctgov` | full | partial | Contextual location mapping; coordinates or explicit admin text outrank city+country fallback |
| `isrctn` | full | partial | crosswalk-confirmed `studyLocation` together with `recruitmentCountry` |
| `aea` | full | none | excluded from confirmed ADM1 results |
| `who-ictrp` | full | none | excluded from confirmed ADM1 results |

For CT.gov, direct country filtering may continue to use the source country
label. ADM1 verification must consume the contextual location crosswalk and
the location evidence from the same source record. Bare city names without country context are insufficient for ADM1 verification.

No approved production polygon dataset exists yet. V2 retains `geoPoint` in
its internal location model and exposes a future boundary-resolver seam, but
Boundary resolution requires a separately reviewed dataset. Where only contextual matching is available, metadata reports partial
ADM1 coverage.

## Limits and budgets

The fixed limit is 100 **unique studies per upstream source request/branch**.
It is applied after every source-supported predicate and before intervention or
outcome rows are expanded for presentation. A canonical study key is the pair
`(source ID, source study ID)`.

Results from independent sources are merged afterward. There is no global
100-row or 100-study cap, so a v2 response may contain more than 100 studies.
For a `state` branch, every supported condition/outcome role predicate and all
shared intervention/region predicates are evaluated before this cap. A source
must not run separately capped condition and outcome searches and union them in
the client or adapter.

Unavoidable fan-out uses these per-source defaults:

| Setting | Default |
|---|---:|
| `REGION_QUERY_MAX_REQUESTS_PER_SOURCE` | 8 |
| `REGION_QUERY_MAX_PAGES_PER_BRANCH` | 3 |
| `REGION_QUERY_TIME_BUDGET_SECONDS` | 5 |

If a budget is exhausted, the adapter returns only the sound subset already
verified and sets both `truncated` and `approximate` to true. It never removes
a requested predicate to obtain more results.

## HTTP response

V2 returns an envelope:

```json
{
  "results": [],
  "meta": {
    "api_version": "query-v2",
    "returned_unique_studies": 0,
    "limit_per_source_branch": 100,
    "truncated": false,
    "approximate": false,
    "sources": {
      "ctgov": {
        "status": "included",
        "coverage": "partial",
        "returned_unique_studies": 0,
        "truncated": false,
        "approximate": false,
        "reason": null
      }
    }
  }
}
```

The complete schema requires an entry for all four canonical sources. Source
status is one of `included`, `excluded`, `unavailable`, or `error`. Coverage is
one of `full`, `partial`, `country_only`, or `none`. A non-null reason is one of
`unsupported_admin_level`, `budget_exhausted`, `upstream_unavailable`, or
`upstream_error`.

Overall `truncated` or `approximate` is true if any contributing source has the
corresponding value. Excluded sources return zero studies and an explicit
reason. Errors and unavailable optional geographic assets are isolated to the
affected source and must not cause HTTP 500 for an otherwise valid request.

## Canonicalization and cache identity

V2 has a cache namespace distinct from v1. Its canonical identity contains:

1. contract version `query-v2`;
2. sorted, de-duplicated URI values for each axis;
3. every per-axis logic value, including defaults;
4. source selection, if source selection is later exposed; and
5. data/taxonomy version inputs needed to invalidate contextual region maps.

Input ordering and duplicate repeated values must not create different cache
entries. Changing an axis logic value must create a different entry.

## Regression and canary gates

Deterministic automated fixtures cover:

- combined multi-axis planning;
- unified State condition/outcome matching, including outcome-only studies;
- State role-union de-duplication before the source-branch limit;
- within-axis OR/AND and cross-axis AND;
- World, regional group, country, ADM1, region OR, and multisite region AND;
- Chittagong not becoming Bangladesh-wide;
- CT.gov contextual evidence and coordinate/explicit-admin precedence;
- ISRCTN partial ADM1 confirmation;
- AEA/WHO ICTRP exclusion from confirmed ADM1 matches;
- de-duplication before the 100-study source-branch limit;
- presentation expansion after that limit;
- sound partial results and explicit metadata on budget exhaustion; and
- missing optional geographic assets without HTTP 500.

The canary runner compares unchanged v1 and v2 in the same run for
Malnutrition anywhere, Malaria anywhere, Malaria plus Chittagong, Bangladesh,
South Asia, and multi-region OR/AND. It records status, latency, payload hash,
unique studies, per-source counts, and v2 coverage/truncation metadata.

Live registry counts change over time. Canary comparisons check same-run
source preservation and whether presentation expansion hides studies, rather
than requiring a historical result count.

## Promotion boundary

The Search interface promotes the first Search selector to the unified `state` axis while
leaving explicit role axes available to API clients. Production deployment
still requires an explicit decision after review.

### Search response deadline

`QUERY_V2_RESPONSE_TIMEOUT_SECONDS` is a positive finite number of seconds,
configurable through Docker Compose/the API environment; its default is **10**.
Restart the API to change it. The route returns sooner when the search finishes.
The budget starts at ASGI request entry and includes planning, cache waiting,
source execution, aggregation and serialization. The route reserves up to 200 ms
within that budget for response preparation, including a final 20 ms margin.
Existing per-source budgets still apply. Network delivery to a slow client is
not included in server-side computation time.

Additive metadata:

- `execution_status`: `complete`, `partial`, or `timeout`.
- `timed_out_sources`: source IDs that exhausted their execution budget.
- `completed_sources`: sources that finished without an execution failure.
- `response_timeout_stage: "serialization"`: exceptional failure to prepare
  the response in time; this is a controlled timeout, not an empty successful search.

At a deadline, completed-source results and fully evaluated Boolean-clause
subsets survive. Unfinished AND intersections never become purported matches.
Sources that did not finish are named in Search's notice. Empty timeout/partial
responses never display the normal no-matches message. Completed empty searches
still do. Timed-out and transiently failed executions are not cached.

Identical callers share execution; each caller has its own remaining wait budget.
A caller disconnect or timeout does not cancel other callers' work. Shared source
execution has its own original deadline, which new callers cannot extend. Pending
source/HTTP cleanup remains owned and observed until it actually exits; its own
context manager retains concurrency permits until then. Cache compression runs
in the background, with subsequent callers coalescing onto the ready result
until the cache entry is stored. It cannot delay a completed search response.

`X-Request-ID` correlates `query_stage` logs for planning, thread queue/execution,
source start/end, aggregation, cache lookup/wait/store, watcher cleanup,
serialization and actual ASGI response start/end. Query-v2 cache stats include
`background_tasks` for operational cleanup checks. These logs use the uvicorn
error logger's INFO configuration and contain no study records.

The deadline is a wall-clock bound enforced by the event loop, not a hard real-time
scheduler guarantee. CPU-heavy planning, aggregation and serialization are moved
off the event loop; keep that property when adding new work. A deadline alone
is not a substitute for diagnosing an underlying delay.
