# Study enrichment contract — 0.6.0

Local implementation awaiting independent review. No extracted production data is
included. `tests/fixtures/enrichment/dual-registry.json` is entirely synthetic.

## Identity and compatibility

`ue:Evidence` remains a registry/source record, as in 0.5.0. Its previous use for
both source artifact metadata and study counting was ambiguous. This change adds
`ue:Study` for the underlying investigation; `ue:studyIdentity` connects verified
records to it. Original URIs, source-qualified IDs, axis and outcome links remain
unchanged. Unverified registrations still count independently. Citation/aboutness
is not identity verification. A DOI belongs to a document, never the study.

Before: registry A and registry B each counted as source-qualified records.
After **verification**: A → S ← B; report R1 → S ← R2; three findings → S, each
with its own R1/R2 locator. Overall count 1; CT.gov 1; WHO ICTRP 1; edge weight 1;
one detail item with both registry links. Registry counts overlap and must not be
summed. A report describing S and T can link to both, without merging S and T.
Reports and additional timepoints never create more studies.

The fixture tests this additive mapping, independent enrichment retrieval,
comparison-specific findings, corrections, withdrawals and retained history.
There is no corpus-wide relabeling or migration of registry records.

## Standards audit and selected mapping

See `standards-pins.json` for original download URLs, SHA-256 hashes, versions and
machine-checked PICO domains/ranges. Cochrane's 4.0.1 documentation is dated
2019-09-06. Its actual RDF namespace is **HTTP**, not HTTPS. These are different
RDF identifiers. 0.6.0 uses HTTP; explicit compatibility axioms cover only the two
HTTPS classes used in UE 0.5.0. No global URI rewriting or redirect assumption.
`SpecificMetric` is a class; `specificMetric` is not a property in that file.

| Purpose | Selected terms and placement |
|---|---|
| Investigation identity | `ue:Study rdfs:subClassOf obo:OBI_0000066` (investigation; a planned process). |
| Document version | `ue:ReportDocument` subclasses `obo:IAO_0000310` (document) and `prov:Entity`. |
| Citation/version/source | `dcterms:identifier`, `bibliographicCitation`, `hasVersion`, `source`, `type`. |
| Aboutness and lineage | `obo:IAO_0000136` relates a document to a study; verified identity evidence is separately mandatory. Findings use `prov:wasDerivedFrom`; corrections use `prov:wasRevisionOf`. |
| Study question | `ue:studyContext` links a study to a `pico:PICO` node. |
| Population | `pico:population`: PICO → Population; `age`: Population → Age; `condition`: Population → Condition. Cochrane Population describes people/patients. Non-human units use `ue:NonHumanPopulation` and `ue:nonHumanPopulation`, without clinical equivalence. |
| Arms | `interventionGroup` / `comparatorGroup`: PICO → InterventionGroup. `intervention`: InterventionGroup → Intervention. Per-study descriptors link to shared taxonomy with `ue:taxonomyConcept`. |
| Intervention descriptors | `dose`, `duration`, `schedule`, `modeOfDelivery`: Intervention → Dose/Duration/Schedule/ModeOfDelivery. All optional, located on per-study descriptors, not taxonomy concepts. |
| Specification | `ue:OutcomeSpecification` subclasses `pico:Outcome`, a desired outcome specification. `outcomeGroup`: PICO → OutcomeGroup; `outcome`: OutcomeGroup → Outcome. |
| Measurement and aggregation | `outcomeMeasurement`: Outcome → AssessmentScale; `aggregationMethod`: Outcome → AggregationMethod. |
| Endpoint | `endpoint` / `primaryEndpoint`: OutcomeGroup → Duration; not properties on an Evidence record or numerical result. |
| Result | `ue:OutcomeMeasurement` subclasses `prov:Entity`, **not** pico:Outcome. It links to its study, optional specification, State/indicator, comparison, subgroup, timing, original result and field-level locators. `ReportedFinding` is retained only as a deprecated compatibility subclass. |
| Statistics | Numerical estimates use `obo:STATO_0000039` (statistic); explicitly reported confidence intervals use `obo:STATO_0000196`. Original values/unit/interval metadata remain literal UE fields. No invented STATO interval properties or forced classification of every uncertainty as a CI. |

OBI/IAO and STATO versions are pinned in the JSON manifest. DCTERMS and PROV-O
provide sufficient citation and derivation terms. FHIR ResearchStudy/related
artifacts was considered as a reference pattern; no FHIR server, BIBFRAME or DCAT
bibliographic layer is needed. No whole-ontology imports are added. UE-specific
links express study identity verification, extraction/eligibility semantics and
field-level locators that the selected minimal external terms do not express.
Complex comparison/timing/evidence objects are explicitly serialized JSON
literals in RDF; the validated canonical TTL is authoritative.

Legacy `ue:OutcomeMeasurement` nodes and their links remain valid, but the class
no longer subclasses pico:Outcome: legacy nodes combine planned and reported
metadata, whereas Cochrane Outcome specifies a desired outcome. It now subclasses
prov:Entity. This semantic correction is deliberate and tested. Existing
`observedDirection` positive/negative/null/mixed values remain accepted; legacy
`null` means no observed change, not missing or nonsignificant. New findings use
increase/decrease/no-change/mixed/uncertain, separately from significance and
benefit/harm. A nonsignificant -1.2 is not zero. No automatic conversion of old
null/mixed values into extracted findings is performed.

## Extraction schema and review

The fixture is a complete valid batch for `study-enrichment-1`. The adapter
`validate_batch` is executable validation: unknown fields, unverified identities,
unsupported field locators, invalid State/indicator mappings, protocols used as
results, inconsistent versions and mutation of existing finding facts fail.
Each registry record has a verification with basis (`explicit-registry-id` or
`verified-match`), source_url, locator, checked_by, checked_at and corroborating
rationale for a verified match. Report links independently require this proof.
Each document has identifier, immutable version/kind, citation, URL, eligibility
status and a dated/reviewed status_check. DOI strings use lowercase `doi:` form.

Each finding names its study, State, measure, mapping justification, comparison
identity/kind/reference/quantity, timing/reference point, sources and status. It
has an original number/unit or a faithful short result_text, never invented nulls.
Every supplied factual field requires an explicit supporting source's `fields`
entry and locator. A derived value needs method/inputs/performed_by. Optional
population/arms and planned measures require their own verified source locator.
Shared State/Indicator classes are not declared equivalent to PICO classes.
Indicator mappings must exist as `ue:measures`; broad growth/search membership
cannot stand in for a Stunting finding.

`review_store` and the Teacher `enrichment review` command expose each finding and
its exact source locators and report validation errors. This is structural review,
not independent verification of the paper. All commits are audited as immutable
batch events. Metadata caps reject oversized work rather than dropping fields:
500 finding versions / 2 MB per study, 20 registry links, 100 context mappings,
100 planned measures and 100 documents per batch. Result quotations are limited
to 2,000 characters; extraction must still respect each source's usage rights.

## Eligibility, querying and counting

A finding qualifies only when active and supported by eligible active document
versions. Retraction/supersession changes invalidate the eligibility index and
cache version atomically. Corrected findings have explicit `replaces` links;
affected old findings become superseded. History is retained and exposed but
cannot qualify default outcome retrieval. Withdrawal does not erase a study's
independent registry-search support or unrelated valid findings. When one of
several sources is withdrawn, active use requires reviewed lineage naming the
remaining documents, reviewer/date/rationale and independently supported fields.

Search v2 unions bounded registry results with eligible outcome-index candidates,
then deduplicates verified identities. `match_provenance.basis=reported_outcome`
and Graph `query_matches.retrieval_basis=reported_outcome` identify enrichment;
they do not claim a registry lexical match or baseline condition. Source
attribution stays CT.gov/ISRCTN/AEA/WHO; publishers are citations, not registries.
State matches condition or outcome; explicit outcome matches outcome; an outcome
does not satisfy condition-only queries. All populated axes and AND/OR operators
must qualify on verified stored context. Unknown locations are not guessed.
World is unrestricted, consistent with canonical query semantics.

`UE_ENRICHMENT_LIMIT` defaults to **100** candidate studies (range 1–1000), matching
the scale of existing source limits. Candidates are eligible RDF-derived outcome
matches, ordered by study URI. The cap applies **before** the remaining context
filters and metadata expansion: a response can underfill and still be truncated.
Coverage explicitly reports limit, returned_unique_studies, truncation and reason.
Read snapshots also have a two-second enrichment budget; interruption returns explicit incomplete/unavailable coverage. This is not an uncapped corpus scan. Existing live/stored source boundaries and
budgets remain; enrichment does not copy registry corpora. Graph additionally
requires a verified mapped intervention. Its aggregates and details use the same
candidate ordering, eligibility, canonical identity and per-source membership
proofs. Changed eligibility/cap invalidates Search cache, Graph cache/detail
identities and signed edge tokens/cursors (projection version 3). Source count
provenance remains separate while overall/node/edge/pagination counts merge only
verified identities. Existing legacy graph design weighting remains a display
heuristic, deduplicated by study; no finding confidence is assigned.

Search cards and Graph edge details show planned measures, active findings,
comparison/timing, original values/uncertainty, report citations/locators, registry
links, enrichment retrieval basis and expandable historical support. The stable `GET /studies/enrichment?study=STUDY_URI` metadata route remains reachable after a study loses active outcome-search eligibility. Graph
connections remain evidence associations, not treatment-effect claims.

## Authoritative TTL storage and workflow state

Knowledge is stored in the project-configured canonical Turtle file, e.g.
`evidence/outcomes.ttl`, not SQLite or a task folder. It contains studies, report
metadata, findings, domain links and immutable revision events. Scalars and
identifiers use RDF literals/resources; complex comparison, timing, identity
proof and provenance objects retain the contract's JSON-literal representation.
`ue:storedFields` records lossless field types for the round-trip adapter; there
is no hidden whole-record JSON payload overriding the semantic finding fields.
The public graph projection and canonical fields are validated for agreement.

Operational claims, frozen queues and receipts live in a separate ignored
`.outcomes.workflow.json`. A filesystem lock and write-ahead transaction journal
protect the TTL/workflow pair. Validation errors publish neither. After an
interrupted publication readers report unavailable; the next writer completes
the recorded transaction before accepting work. Reserved IATI requests are never
refunded on uncertain recovery. Do not manually modify files during worker runs
or delete journals to bypass recovery. Stop workers and review before restoration.

Set `UE_ENRICHMENT_TTL` for the API and loader; `UE_ACTORS_TTL` names the activity
TTL. SQLite environment variables are no longer supported. Unset means disabled;
a configured missing/corrupt file is unavailable, not an empty evidence result.
API adapters read immutable RDF snapshots and apply the same eligibility,
counting, caps, identity and history rules. The existing loader publishes the
configured files into `/graph/outcomes` and `/graph/activities` named graphs.
The original registry graphs remain source mirrors. Local TTL edits do not
implicitly publish into Fuseki or deploy an application. Use the reviewed normal
loader/deployment process and matching snapshot configuration for publication.

Commands (all paths are project configuration, not fixed task paths):

```sh
python scripts/evidence_enrichment.py init --store evidence/outcomes.ttl
python scripts/evidence_enrichment.py review --store evidence/outcomes.ttl
python scripts/evidence_enrichment.py backup --store evidence/outcomes.ttl --output BACKUP/outcomes.ttl
```

Backup includes the paired operational file. Restore the pair while workers are
stopped. The one-time `migrate_legacy_enrichment.py --source OLD.sqlite
--destination evidence/outcomes.ttl` reads legacy data read-only, preserves all
knowledge/history and workflow receipts, refuses active claims, and validates
round-trip equality. Retain the old file as a historical backup; no runtime code
reads it. Migration does not infer missing indicators or alter extracted effects.

## Indicator coverage and comparison mappings

New findings require `indicator_uri`, or `indicator_review` with
`status: unresolved`, rationale and next_step. Existing migrated findings can
retain their missing mappings pending explicit review. Teacher must not report
fully enriched work with unresolved indicator coverage. Check the Indicator's
class and `ue:measures` applicability, threshold, denominator, scale and population.
State and Indicator additions go through the core thesaurus validation workflow.
An Indicator is never made a narrower State. Threshold subtypes belong in State
hierarchy; broader queries may include narrower States without equating metrics.

Optional `comparison.intervention_concepts` and `comparator_concepts` hold
canonical Intervention URIs with `taxonomy_rationale`. They export as
`ue:comparisonIntervention` and `ue:comparisonComparator` on the finding. These
are comparison-specific; they do not assign every study intervention to every
finding. Missing mappings remain a review gap. The stored original effect,
comparison, subgroup and follow-up are necessary before comparing magnitudes.
A submission `report_inventory` is preserved in its receipt. Each selected
document may store `discovery` steps with source_url, actual linked_url when
present, accessed_url, method, version_relation, checked_at and rationale.
Report discovery inventories must distinguish the actual registry outbound link,
broken-link searches, replacement copies and different publication versions.

## Safe updates and frozen work (Task 255 review corrections)

Read the study history endpoint or adapter `revision_preconditions(conn, study_id)`
**before** editing existing metadata. Search also carries this `preconditions`
snapshot; Teacher freeze and claim preserve it alongside every verified registry
record (source, identifier, URL, record URI and verification), full canonical query,
coverage metadata, timestamp and response hash. Source display names normalize to
source keys. A merged study stays one work item containing all its registry links.

Include the original `preconditions` in the submitted batch: three maps named
`studies`, `documents`, `findings`, each keyed by entity URI with a SHA-256 hash of
its stored payload. Any changed existing entity requires its matching prior hash;
a correction also requires the prior finding's hash. New entities need no token.
Do not fetch fresh hashes merely to make an old submission pass. On rejection,
inspect and reconcile the newer evidence, then prepare a newly reviewed submission.
All checks happen inside the locked file transaction before knowledge changes,
version events or receipts. Failed submissions retain the claim for reconciliation.
Exact already-committed batch retries are no-ops, including after later withdrawal;
coordinator receipt retries require the original token, owner and submission hash.

Study updates are patches by default: omitted optional fields survive; omitted
context members also survive. Supplied arrays replace that array, not append to it.
Removing existing metadata (including array entries) requires both the current
study hash and `study_replacement_review` with `reviewed_by`, timezone-qualified
`reviewed_at`, and `rationale`. Supplying this review selects explicit **full study
replacement**: provide every field you intend to retain. Original registry IDs and
record URIs still cannot be erased. The submitted review and change stay in audit
history. Documents/findings remain complete entity submissions, revision checked.

Status changes additionally require
`eligibility_reviews.documents[DOCUMENT_URI]` or
`eligibility_reviews.findings[FINDING_URI]`, containing `from`, `to`, `reviewed_by`,
`reviewed_at`, and `rationale`. Document status checks may never move backwards;
a status transition requires a strictly newer check and a review at or after that
check. A legitimate reviewed reinstatement is supported; stale worker snapshots
cannot automatically reactivate withdrawn evidence. Finding facts remain immutable;
create a linked correction to change reported values.

The existing extractor uses `type=pre-specified-measure`, `reported-result`, and
`adverse-event`. Enrichment and both UIs recognize these as well as explicit
`outcome_status`/`outcomeStatus`. Only explicit plans enter `planned_outcomes`.
Posted results/adverse events remain intact under `source_reported_outcomes`, shown
as registry-reported and not yet verified. Unknown legacy entries remain intact
under `source_unspecified_outcomes`. Raw `outcomes` also remain available. Neither
category is promoted into Teacher findings, outcome retrieval, or graph support.
UI fallback applies the same partition when enrichment is disabled or unavailable.


## OutcomeMeasurement reconciliation

This section supersedes the earlier recommendation to write a separate
ReportedFinding class. The preceding command, JSON-batch and persistence notes
describe the current application/Teacher implementation; they are not a claim
that its data has already been migrated. This change is schema-only.

Canonical result pattern:

```turtle
:result a ue:OutcomeMeasurement ;
    ue:outcomeStatus "reported" ;
    ue:measurementStudy :study ;
    ue:ofState state:Stunting ;
    ue:ofIndicator state:SDG221UnderFiveStuntingPrevalence ;
    ue:measurementStatus "active" ;
    ue:measure "Stunting odds ratio" ;
    ue:comparison "Supplement versus control; fully adjusted model" ;
    ue:timing "After 18 months" ;
    ue:value 0.63 ; ue:unit "odds ratio" ;
    ue:observedDirection "negative" ;
    prov:wasDerivedFrom :report ; ue:sourceLocator :locator .
```

This fragment requires separately declared Study, State, Indicator, ReportDocument
and locator records to validate. The full synthetic fixture supplies those records.
Complex comparison/timing metadata can remain JSON literals under the existing
storage contract. This reconciliation does not assert FHIR conformance or replace
those literals with FHIR objects.

| Previous term | Preferred term | Compatibility |
| --- | --- | --- |
| ReportedFinding | OutcomeMeasurement + outcomeStatus reported | Deprecated subclass, not equivalence. No implicit conversion of plans to results. |
| findingStudy | measurementStudy | Deprecated subproperty; both accepted by reported-result shapes. |
| outcomeState | ofState | Deprecated subproperty; aliases cannot identify different States on a canonical result. |
| findingIndicator | ofIndicator | Deprecated subproperty; optional but type checked when present. |
| findingStatus | measurementStatus | Deprecated subproperty; active/superseded/retracted is revision status, not planned/reported/synthesized. |
| direction | observedDirection | NOT an equivalent property: value vocabularies differ. Explicit conversion below. |

Direction conversion: increase → positive; decrease → negative; no-change → null;
mixed → mixed; uncertain → uncertain. These describe the measured quantity, not
benefit/harm. Nonsignificance does not convert a nonzero estimate to null or
uncertain. Historical null retains its explicit no-observed-change meaning.

`OutcomeMeasurement` granularity is one measure/indicator, comparison, population
or subgroup, follow-up and analysis. Different adjustment models and effect scales
can produce distinct records with the same Study/Indicator links. Never count
these as independent studies. Record identifiers need not change, including existing
paths containing `/findings/`. ReportDocument and Study remain separate classes.

Plans retain the existing thin contract; no results or report provenance are
invented. Explicitly reported canonical measurements require study linkage,
comparison, timing, measured State, source report and locator, revision status,
and either a numerical result with unit or faithful result text. `ofIndicator`
remains optional at core-ontology level; the Teacher extraction policy is stricter.
Synthesized measurements remain supported by the original basic contract, but
this release does not define a complete synthesis model or force one-study identity.

`OutcomeSpecification`/PICO question context may be used where useful but is
optional. The HTTP PICO namespace and existing compatibility axioms remain.
A future UE FHIR profile/export and application migration are separate work.

Migration acceptance: read old and new representations during transition, emit
canonical terms after deployment, preserve every source/estimate/history/identity
and workflow receipt, run unchanged-data validation, and retire compatibility
readers only after audit. Do not manually replace strings in authoritative TTL:
its field manifests, typed round-trip adapter and paired workflow hash must be
migrated transactionally. The ontology does not perform this storage migration.


## CIDS alignment and canonical result serialization — 2026-10-05

The adapter writes `ue:OutcomeMeasurement`, `measurementStudy`, `ofState`,
`ofIndicator`, and `measurementStatus`. Existing URI identities are preserved;
legacy finding-class/property Turtle is still readable, but new writes use the
preferred terms. Internal submission keys and API `reported_findings` remain
compatible. `observedDirection` explicitly maps increase/decrease/no-change to
positive/negative/null; the original extraction `direction` is retained losslessly.

Every result links through `ue:contextualOutcome` to a study-specific
`cids:Outcome`; indicator-mapped results also use `ue:contextualIndicator` to a
study-specific `cids:Indicator`. CIDS `hasIndicator`/`forOutcome` connect them.
CIDS `Code` with `hasSpecification` (an xsd:anyURI literal, as published) identifies
the original State and Indicator thesaurus concepts. These contextual nodes are
not new thesaurus concepts or additional studies. There are no global class
equivalence claims. Superseded results retain their own original mappings;
retrieval must still filter result/report eligibility.

Numerical statistics also use ISO 21972 `Measure`/`numerical_value`. STATO identifies
statistics, confidence intervals, explicitly labelled odds ratios and mean
differences. Other effect types retain their faithful units and generic statistic
type until a justified mapping is added. An SD unit alone does not establish a
standardized mean difference. Confidence intervals retain existing interval fields.

CIDS `IndicatorReport` is appropriate for a directly reported indicator level,
not automatically for every comparison result. Existing Stunting extractions are
effects, changes, associations or text results, so this migration does not create
raw indicator reports or fabricate arm values. In particular, Zambia OR 0.63 is
not prevalence 0.63. The two descriptive prevalence values in the isolated Zambia
prototype are not added by this migration. A future extraction of raw arm values
needs explicit semantic fields and validation before the adapter emits those
IndicatorReports. Teacher must not relabel a comparison to force CIDS coverage.

The existing enrichment skill reads this project contract; workers continue using
the same validated submissions. Canonical serialization performs the alignment,
so subsequent writes retain CIDS rather than requiring manual triples from workers.
No FHIR server or additional database is required. This is a UE application
profile reusing CIDS 3.2.1 terms, not certification at a CIDS reporting tier.
Organizational obligations are not filled with invented data. Source metadata,
comparison, timing, subgroup, values, uncertainty, review state and history survive
unchanged. A migration must round-trip the full payload and workflow under the
existing lock/journal protocol, preserve the study count and fail on concurrent
changes or active worker claims.
