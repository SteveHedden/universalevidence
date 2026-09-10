# Universal Evidence

Universal Evidence is an evidence search engine for people designing, funding and delivering development programs. Search for research on a condition or outcome, an intervention and a place, then follow the results to the original registry records.

- [Use the hosted application](https://www.universalevidence.com/)
- [Explore the API](https://api.universalevidence.com/docs)

## What it does

UE aligns terminology across four registries using shared vocabularies. Search supports broader and narrower concepts, multiple selections, and geographic filtering. Graph shows connections between states and interventions supported by study records.

| Registry | Retrieval mode |
|---|---|
| AEA RCT Registry | Stored snapshot in Fuseki |
| WHO ICTRP | Stored snapshot in Fuseki |
| ClinicalTrials.gov | Live API |
| ISRCTN | Live API |

A study appearing in a result means it is associated with the selected concepts. It does **not** establish that an intervention was effective. Registry records may describe planned studies without reported results. Mapping coverage, source availability and query limits affect what is discoverable; no matches does not prove that no evidence exists. Registrations across different sources may describe the same underlying study.

## Code and data availability

The application code is open source. The complete hosted dataset is **not included**. A working backend requires separately provisioned registry snapshots, geographic mirrors and crosswalks. Without them, container builds can succeed but API startup cannot complete. A self-contained demo and independent data-provisioning guide are planned.

The reusable ontology and thesauri are available separately:

- [Ontology](https://github.com/SteveHedden/universalevidence-ontology)
- [Conditions and Outcomes thesaurus](https://github.com/SteveHedden/universalevidence-conditions-outcomes-thesaurus)
- [Interventions](https://github.com/SteveHedden/universalevidence-interventions-thesaurus)

Application code uses [Apache 2.0](LICENSE). Bundled ontology and vocabulary content has separate [CC BY 4.0 licensing](LICENSE-DATA). Registry records and dependencies retain their own terms; see [data licensing and attribution](DATA-LICENSING.md) and [NOTICE](NOTICE).

## Architecture

The React/Vite frontend calls a FastAPI backend. Apache Jena Fuseki stores the core vocabularies and the AEA/WHO snapshots. The loader combines source mappings with vocabulary hierarchies to create search indexes; ClinicalTrials.gov and ISRCTN are queried at request time.

`vocabularies/regions.ttl` contains UE regional groupings. Country and first-level administrative-area concepts come from separate GeoNames mirrors. The source configuration in `vocabularies/sources.ttl` describes registry fields and mapping inputs.

## Development setup

Use Python 3.11 (the container runtime), Node.js 24 or newer, npm, and Docker with Compose v2 for the backend stack. Python dependencies are currently unpinned; the frontend uses `site/package-lock.json`.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

If you already have the required data inputs, consult [deployment requirements](deploy/VPS_SETUP.md) before starting the backend. Compose can download configured inputs from authorized R2 storage; it does not grant access to the hosted service's storage. Keep credentials in a local `.env`, using `.env.example` as the configuration reference.

For an already initialized local Fuseki instance, start the API from the repository root:

```bash
FUSEKI_URL=http://localhost:3030/ue/query .venv/bin/uvicorn api.main:app --reload
```

The API preloads required mappings during startup. An empty or unavailable crosswalk can prevent startup; do not disable that check to present incomplete mappings as a ready service.

For the frontend, configure `site/.env.local` with `VITE_API_BASE_URL=http://localhost:8000`, then:

```bash
cd site
npm ci
npm run dev
```

See the [frontend guide](site/README.md) for build and hosting details. A frontend without a working API does not provide a standalone search service.

## API examples

Queries use full concept URIs. This example searches the hosted service:

```bash
curl --get 'https://api.universalevidence.com/query/v2' \
  --data-urlencode 'state=https://universalevidence.com/vocab/states/Malaria'
```

Add `intervention` or `region` to restrict the query. Repeat a parameter for multiple concepts. Within an axis, the default is OR; use `state_logic=and`, `intervention_logic=and` or `region_logic=and` to require all selected concepts. Different axes are combined with AND. `state` matches condition or outcome topics; explicit `condition` and `outcome` restrict that role.

| Endpoint | Purpose |
|---|---|
| `/query/v2` | Study results and source/coverage metadata |
| `/graph/v2` | State/intervention nodes, evidence edges and metadata |
| `/taxonomy/condition?q=malaria` | State concept autocomplete; the route retains the name `condition` |
| `/taxonomy/condition/tree` | State hierarchy |
| `/ontology/State` | Browser-readable class documentation |
| `/docs`, `/openapi.json` | Interactive API reference and machine-readable schema |
| `/health` | API process readiness; not a guarantee that every upstream registry is available |

Search responses contain `results` and `meta`. Inspect `meta.sources`, `truncated`, `approximate` and `execution_status` before interpreting results as complete. Search has a configurable default 10-second server budget and preserves completed-source results when others fail or time out. A study may appear in multiple intervention groups; presentation rows are not unique-study counts. Limits apply per source branch, and Graph has additional presentation limits. See the [Search API reference](contracts/query-v2/README.md) for details; the legacy `/query` and `/graph` routes have different response formats.

## Tests and builds

Frontend tests run without registry data:

```bash
cd site
npm ci
npm run test:run
VITE_API_BASE_URL=/api npm run build
```

The build writes static files to `site/dist/` and includes dependency license notices. `/api` assumes a same-origin reverse proxy that strips this prefix before forwarding to FastAPI.

Selected backend conformance checks run without the private registry snapshots:

```bash
.venv/bin/python -m pytest -q tests/test_vocabulary_measurements.py tests/test_dependency_notices.py
```

The broader backend suite is not yet fully self-contained. Some tests require separately supplied crosswalks, geographic mirrors or registry snapshots; older integration tests also contain obsolete expectations. Running the entire suite without those inputs will fail. Fixture provenance is documented in [tests/fixtures/README.md](tests/fixtures/README.md).

Container builds collect Python dependency license notices. Build success alone does not validate dataset completeness or live registry access. See [deployment requirements and verification](deploy/VPS_SETUP.md) before operating a persistent instance.
