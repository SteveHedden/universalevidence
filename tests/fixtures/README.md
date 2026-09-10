# Test fixture provenance

The fixtures below are constructed test inputs, not authoritative registry exports or evidence of study results. Registry-shaped identifiers are opaque test tokens; they may coincide with real identifiers and must not be resolved or cited as real studies. Country names, terminology and vocabulary URIs retain their normal meanings and upstream attribution.

UE-authored test content is covered by the repository's Apache 2.0 license. Referenced vocabulary concepts retain their separate licenses; the fixture license does not relicense registry datasets.

| Files | Provenance and purpose |
|---|---|
| `ctgov_child_malnutrition_kenya.json` | Synthetic minimal ClinicalTrials.gov-shaped payload. The former real title/identifier were replaced; exercises response parsing. |
| `ctgov_child_malnutrition_kenya_outcomes.json` | Constructed response with illustrative outcome and adverse-event values; numbers are not findings from a study. |
| `isrctn_empty.xml`, `isrctn_query_default.xml` | Constructed XML parser examples, including a sentinel category and fictional country; not downloads from ISRCTN. |
| `who-ictrp/child-malnutrition.csv`, `who-ictrp/wash.csv` | Constructed CSV parser/deduplication examples, including a fictional country and duplicated test identifier. The directory describes the expected format, not the origin of these records. |
| `query_v2/source_adapter_cases.json` | Constructed adapter cases and geographic expectations; no actual trial findings. Geographic identifiers refer to GeoNames. |
| `query_v2/canary_queries.json` | UE-authored query scenarios and acceptance criteria, with a historical UE count as diagnostic context; not a registry-record dataset. |
| `ontology_conformance/normalized-studies.ttl`, `ontology_conformance/normalization-notes.json` | Wholly synthetic conformance examples using example.org subjects and SYNTHETIC identifiers. Source names select representative formats only. Tests validate required source/study identifiers and condition typing. |

The previous real-record normalization examples and source-derived crosswalk review inputs are retained privately, outside the public fixture set. The crosswalk audit remains a separate private test; it is not required to validate the public synthetic records.
