> Storage update (2026-10-05): canonical `evidence/activities.ttl` replaces the
> SQLite pilot store. Use `UE_ACTORS_TTL`. Claims, receipts, operation idempotency
> and in-flight discovery ownership live in the ignored paired workflow JSON;
> public coverage remains in RDF. See `study-enrichment.md` for file transaction,
> crash recovery and loader behavior. Use the configured TTL path for all commands below.

# Bounded organization/activity population contract

This is an API-only IATI capability, isolated from research Search/Graph and study
outcomes. It is opt-in through `UE_ACTORS_TTL`. No live population or deployment is
included in the implementation. Default planned Stunting discovery cap: 25 unique
activities across all queries; plan also records query and request caps. Storage
has a hard 1,000-activity safety bound; it is not a whole-corpus harvester.

## Standards and model

Reviewed IATI 2.03 references (2026-10-04):

- [participating-org](https://iatistandard.org/en/iati-standard/203/activity-standard/iati-activities/iati-activity/participating-org/)
- [organization roles](https://iatistandard.org/en/iati-standard/203/codelists/organisationrole/)
- [sector](https://iatistandard.org/en/iati-standard/203/activity-standard/iati-activities/iati-activity/sector/)
- [Datastore nested JSON](https://docs.datastore.iatistandard.org/en/latest/api/)
- [API access](https://reference.iatistandard.org/en/iati-tools-and-resources/api-gateway/)
- [PROV-O](https://www.w3.org/TR/prov-o/)

The connecting record is the published IATI activity identifier. A participant's
published role belongs to that activity, whose State/Intervention mappings require
review. Reporting metadata is retained separately. No participation or mapping
constitutes evidence of effectiveness or creates a research study.

Canonical UE ontology audit: existing `ue:Evidence`, `ue:Study` and report/finding
terms describe research records and are unsuitable for these activities. Existing
`ue:condition`/`ue:intervention` domains are not reinterpreted. The participation model reuses standard terms. Storage record and revision
classes are declared in the UE ontology; research semantics remain unchanged. RDF exports reuse
`prov:Organization` for named organization instances, `prov:Activity` for activity
instances and the standard `prov:qualifiedAssociation`/`prov:Association` pattern
with `prov:agent` and `prov:hadRole`. Role instances retain their IATI code/label and
codelist source. This is a small standard association, not a new Participation
class or project-management model. Accepted activity topics use `dcterms:subject`;
reified mapping statements carry source snapshot and review metadata. They are
not assertions of organization-specific delivery. Export has no research Evidence,
Study or ReportedFinding instances.

`vocabularies/agents.ttl` declares a flat dataset. Exports populate it with
organization instances, stable hash URIs derived from exact usable published refs,
identifiers, source links, labels and aliases; no class hierarchy or people.
A name-only record's key includes the source activity, publisher and full nested
participant entry. Exact repeated entries deduplicate; different roles remain
separate records. Withheld entries remain unresolved even with refs. Ref-only
organizations have `name_unavailable=true`. Human identity resolutions require a
usable ref, reviewer/date, rationale and source URL; they retain source lineage
and are never allowed for withheld participants.

Nested `iati_json` is mandatory; flattened parallel arrays cannot reconstruct
associations reliably. Keep activity/transaction sector context, code/vocabulary/
URI/version, publisher scope for vocabularies 98/99, narrative and percentage.
Preserve published status/dates/geography and report missing/conflicting metadata.
No `currently_operating` inference is made. Conditions are not used as UE States.
Broad or bundled sector categories do not automatically map to an intervention.

## Persistence and update semantics

`scripts/iati_actors.py` is the project adapter. A separate TTL store is the
source of truth for bounded activity metadata, mappings and identity decisions.
Organization/association triples and their supporting activity records share this
canonical RDF file. Filtered public exports expose eligible support. Schema
`iati-actors-ttl-1` identifies the RDF storage contract. Unknown schema versions
fail; existing research datasets are not migrated. The API
opens the store read-only and never creates it. Missing configuration/store
returns HTTP 503; it does not silently imply zero organizations.

Every write uses `an exclusive file lock` and a read-before-edit activity revision. A
claim freezes that revision, and its commit cannot substitute a fresh token.
Source, roles, identities, mapping eligibility, history, generation and receipts
publish atomically. There are no actor caches or independently materialized count
indexes; lookup counts are calculated from the same read snapshot. Pagination
is bound to generation and filters; changed populations require restarting it.

Source snapshots retain publisher update markers, retrieval times and content
fingerprints. Older comparable publisher timestamps are rejected. Equal/missing
markers do not prove ordering: changed full responses explicitly disclose
`changed-without-publisher-ordering-evidence`; retrieval time is never described
as proof that content is newer. A successful complete per-activity refresh is the
source of current observed participation. It preserves its previous snapshot.
A failed/partial/missing response records bounded error metadata and preserves
last-known data. Search absence never deletes participants or activities.

Mapping support records exact normalized source paths and values. Title/description
support can cite a complete entry or a nested text field. Sector support cites the
whole entry to retain vocabulary/publisher context; code decisions also require
scoped crosswalk review metadata. If support changes or disappears, the current
mapping becomes `needs-review` and stops qualifying lookup; its old accepted
decision remains historical. Unrelated edits and participation changes preserve
unchanged supported mappings. Removed participants/roles stop qualifying; new
participants inherit only the published activity-level relationship. Re-review
uses a fresh claim after inspecting the changed source. Explicit withdrawal needs
source URL, reviewer/date and rationale, and excludes the activity from lookup.

## API

All routes are GET, do not call IATI, and use the same `UE_ACTORS_TTL` file:

- `/actors/organizations?state=CANONICAL_URI&intervention=CANONICAL_URI&role=4`
- `/actors/unresolved-participants` with the same filters
- `/actors/activities/detail?id=IATI_ID&history=true`
- `/actors/vocabulary` returns current identified organization instances in Turtle

At least one axis is required. Matching uses exact canonical URIs, without implicit
broader/descendant expansion. Combined axes and role must be supported by the same
activity. Roles 1/2/3/4 are funding/accountable/extending/implementing; omitting the
filter retains published participation roles. The default result collection and
pagination contain only identified organizations, deduplicated across activities
and roles. Unresolved records have separate pagination. Both endpoints expose
`identified_organization_count` and `unresolved_participant_count` using the same
filters. Unresolved counts describe source participant records, never distinct
real-world organizations.

Pages sort by stable entity ID. Use `limit` (1–100), `offset`, and the returned
`snapshot` when fetching subsequent pages. Missing/stale snapshot for a later page
returns 409. Invalid filters return 422 and unknown activity detail returns 404.
Response metadata discloses every population's State scope, query provenance,
caps, errors, truncation and discovery timestamps, plus current population update
time. The roster is always labeled non-exhaustive. Only activities with an accepted
mapping to their population State qualify; Intervention-only results therefore
remain within that population. Pending/rejected candidates remain inspectable
through workflow status and activity detail.

## Worker submission

The private reusable Teacher lane loads this adapter from project configuration.
A decision includes `axis`, `concept`, `status` (accepted/rejected/ambiguous),
`method` (source-text-review/sector-code-review), `reviewer`, `reviewed_at`,
`rationale` and `support: [{path: [...], value: ...}]`. Example support path:
`["descriptions", 0, "narrative", 0, "text()"]`. Accepted decisions require exact
existing nonempty support. Semantic correctness is established by source review,
not by string equality; automated validation cannot substitute for data review.

No mapping is accepted by discovery. Claim/commit receipts support retries and
crash rollback. Workflow sidecars are isolated from outcome and hygiene queues.
Credentials use `IATI_API_KEY` from the existing process secret environment; no
credentials, headers or full error bodies enter manifests, fixtures or logs.

## Backup and rollback

Stop writers before moving/restoring a store. `python scripts/iati_actors.py backup
--store STORE --output NEW_BACKUP` copies the canonical TTL and its paired workflow sidecar under the file lock,
including source/mapping/receipt history. `export-agents` and `export-activities` write RDF
using `--output`. Restore a reviewed backup with compatible code, or unset
`UE_ACTORS_TTL` to disable API access. No deployment, namespace proxy or Fuseki
changes are performed by these commands. Private run configurations, source sample,
review submissions and populated store remain outside the public application.
