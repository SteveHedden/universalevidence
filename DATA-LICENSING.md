# Code, vocabularies and source data

UE application code is licensed under [Apache 2.0](LICENSE). That license does not replace the licenses or terms of third-party libraries, vocabulary content or study records.

## Bundled vocabularies

`ontology/ue.ttl`, `vocabularies/states.ttl`, `vocabularies/interventions.ttl` and `vocabularies/subjects.ttl` use [CC BY 4.0](LICENSE-DATA). Attribute Universal Evidence and Steve Hedden, identify the version, link to the relevant repository, and indicate changes. Preserve [upstream attribution](vocabularies/ATTRIBUTION.md), including the NLM acknowledgment and snapshot-currency notice. These files are edited vocabulary snapshots, not copies of every linked upstream classification.

The separate releases contain their own source manifests and attribution:

- [Ontology](https://github.com/SteveHedden/universalevidence-ontology)
- [Conditions and Outcomes thesaurus](https://github.com/SteveHedden/universalevidence-conditions-outcomes-thesaurus)
- [Interventions](https://github.com/SteveHedden/universalevidence-interventions-thesaurus)

The UE-authored regions, source-configuration and bearer-map files also use CC BY 4.0; see the [file-level vocabulary licenses](vocabularies/LICENSE.md). This does not change the licenses of the datasets described by those files.

## Registry data

| Source | Applicable terms | Reuse boundary |
|---|---|---|
| AEA RCT Registry Dataverse snapshot | [Dataset DOI 10.7910/DVN/O67RK3](https://doi.org/10.7910/DVN/O67RK3), CC0 1.0 | The checked dataset version permits reuse. Retain dataset/version provenance; this does not license separately linked papers or attachments. |
| ClinicalTrials.gov | [Data terms](https://clinicaltrials.gov/about-site/terms-conditions) | Preserve attribution, processing dates and changes; respect currency requirements and third-party rights. Do not describe the entire database as Apache-licensed or unconditionally public domain. |
| ISRCTN | [Terms, section 7](https://www.isrctn.com/page/terms) | Post-2019 contribution metadata is CC0. Intervention descriptions, outcome measures and other specified narrative fields are CC BY. Preserve record links, credit and applicable record licenses when retaining such text. |
| WHO ICTRP | [Download/data-use terms](https://www.who.int/tools/clinical-trials-registry-platform/network/who-data-set/downloading-records-from-the-ictrp-database) | Commercial use is prohibited by the stated terms. Distribution requires attribution, currency and processing-date information. UE's code license grants no additional rights to this data. |

An API response, mirror, RDF conversion or crosswalk may retain source text and its obligations. A mapping assertion and the copied source wording are distinct contributions. There is no blanket open-license grant for the combined graph or all crosswalk files. Moving data outside Git does not change the rules governing its use in a hosted service.

## Geographic and reference data

[GeoNames](https://www.geonames.org/about.html) supplies geographic identifiers and data under CC BY 4.0. Preserve attribution and identify modifications in distributed mirrors. UE's `regions.ttl` contains regional groupings and links to geographic identifiers; it is distinct from the external country/ADM1 mirrors.

[World Bank datasets](https://datacatalog.worldbank.org/public-licenses) have dataset-specific licenses, commonly CC BY 4.0 with possible third-party exceptions. Retain the exact dataset/version terms when distributing data. External URI links to UN, OECD or WHO classifications do not license redistribution of their complete datasets. See the bundled vocabulary attribution for the actual sources used.

## Libraries and examples

The graph page bundles D3 v7.9.0 under ISC; its [license notice](site/public/third-party/D3-LICENSE.txt) accompanies it. Python and JavaScript dependencies retain their own licenses and notices, including when distributing built assets or container images.

Synthetic examples should be labeled as synthetic. Real or edited study fixtures retain source provenance and applicable terms; they are not automatically covered by the application-code license.

Fixture origins and interpretation are documented in [tests/fixtures/README.md](tests/fixtures/README.md).

## Notices in built distributions

`npm run build` generates `site/public/third-party/DEPENDENCY-NOTICES.txt` from the installed frontend runtime dependencies and copies it, together with D3's license, into the built site's `third-party/` directory.

The API/loader Docker build generates `/app/third-party/python-notices.txt` and its JSON inventory from the installed requirements and their transitive dependencies. Missing packaged license texts fail notice generation. The Python base image and complete Fuseki distribution retain their own supplied operating-system, JVM and server licenses; these are not relicensed by UE. Build tools retain their package licenses even when not included in browser assets.
