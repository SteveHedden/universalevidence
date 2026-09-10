# Universal Evidence ontology 0.5.0

`ue.ttl` defines the classes, properties and embedded SHACL contracts.

An Indicator identifies one or more evidence-supported State targets through `ue:measures` or inverse `ue:measuredBy`. Measurement applicability is not a diagnosis: fetal weight may inform assessment of restricted or excessive fetal growth without asserting either condition. A particular outcome names its topic through `ue:ofState`.

Validate with a SHACL-SPARQL validator and load reference vocabulary types with the data. Warnings are advisory; violations fail. CrosswalkEntry and Region do not have comprehensive standalone shapes; structural conformance is not a scientific mapping audit.

Raw AEA/WHO records use source-native provenance and do not satisfy the normalized Evidence contract. Legacy CT.gov/ISRCTN Study records are not EvidenceShape targets. The four synthetic records in `tests/fixtures/ontology_conformance` exercise the normalized Evidence contract; they do not demonstrate a production normalization migration. Synthetic outcome fixtures exercise contracts absent from the current raw record population.

Ontology content is CC BY 4.0; application code remains under its separate license.
