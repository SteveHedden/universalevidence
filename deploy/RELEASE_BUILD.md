# Reconciled application build

This checkout combines the deployed API source with the maintained search and
editorial source. Keep research authoring and unfinished work on their preserved
branches; do not replace this checkout with the authoring directory.

## Build

Use Python 3.11 for the production image. `requirements-production.lock` records
the package versions captured from the running production image.

```sh
python -m pip install -r requirements-production.lock
cd site
npm ci
npm run build
npm run test:run
```

The build creates `site/dist/index.html` for the landing page and
`site/dist/explore.html` for search, plus About, Open standards, the journal,
assets, robots.txt, and the complete sitemap. Production API calls use `/api`.
Do not overwrite the landing page with the search app or delete retained assets
and redirect destinations during publication.

## Runtime data

A Git checkout alone is not a complete production build context. Restore the
checksummed private runtime bundle before building Docker: the pinned taxonomy
snapshot, release loader, runtime evidence and data, bundled crosswalks, and
study implementation configuration. These are deliberately excluded from Git.
Do not substitute newer research data during a code-only reconciliation.

The image can then be built with the repository Dockerfile. Keep the current
production loader image pinned separately; building the API does not authorize
a data reload.

## Deployment configuration

The reconciled server configuration is one normalized Compose file. It retains
the actual existing data/manifest volume names, read-only API manifest mount,
IATI quota bind, result graph pin, credentials, and separate loader image.
Credential-bearing files stay mode 0600 on the server and out of Git.

Compare `docker compose config --format json` with the captured effective
configuration before activation. Activate only the API using
`up -d --no-deps --no-build api`; preserve the prior Compose invocation and image
for rollback. Never run the loader as part of an API-only release.

## Validation limits

The reconciliation compares against the existing production baseline rather
than assuming the historical test suite is entirely green. Record identical
baseline failures explicitly; do not weaken runtime behavior to satisfy stale
expectations. A prepared candidate is not evidence of deployment.

## Guarded release entry point

Use `deploy/release.py` for code-only releases of an existing installation.
The GitHub manual workflow intentionally fails with operator instructions and has
no SSH or production credentials. It does not deploy on push. The former
pull/build/copy/prune workflow is retired. Do not activate a release through that
old procedure or through the initial-install commands in VPS_SETUP.md.

Prepare and review the build on an isolated checkout at an exact committed SHA.
Restore the private runtime bundle and validate its existing contents manifest
before building. Record the bundle SHA-256, source SHA, image ID and static-file
hashes. Build the image with these provenance labels:

```sh
docker build --label org.opencontainers.image.revision="$RELEASE_COMMIT" \
  --label com.universalevidence.runtime-sha256="$RUNTIME_SHA256" \
  -t "$RELEASE_IMAGE" .
```

Those variables are operator-supplied, verified build inputs. The image must be
available on the release host before activation. The entry point never builds,
pulls, extracts the bundle, fetches source, starts dependencies, runs the loader,
prunes Docker, or changes data. Existing images without these labels must first
be rebuilt from the verified source and runtime bundle.

Prepare a **private mode-0600 JSON plan** on the host. Never commit real plans,
effective Compose, credentials, bundle contents or host-specific paths. Required
schema-1 fields:

| Field | Meaning |
| --- | --- |
| `schema` | Integer `1` |
| `project`, `api_container` | Existing Compose project and API container name |
| `source_checkout`, `source_commit` | Absolute checkout path and full 40-character Git SHA; tracked files must be clean |
| `runtime_bundle`, `runtime_sha256` | Absolute private bundle path and verified SHA-256 |
| `old_image`, `new_image` | Immutable local `sha256:…` Docker image IDs |
| `loader_image` | Exact existing separately pinned loader image reference; unchanged |
| `current_compose`, `current_compose_sha256` | Absolute single normalized active Compose path and SHA-256 |
| `candidate_compose`, `candidate_compose_sha256` | Absolute mode-0600 candidate Compose and SHA-256 |
| `lock_file` | **Existing shared deployment lock from the private operations runbook**, used by all operators; never a new per-release lock |
| `receipt_dir` | New absolute private release directory, outside source/staging/webroot; must not already exist |
| `webroot`, `static_root` | Absolute live and prepared static roots; symlinks rejected |
| `static` | Nonempty mapping of relative paths to entries below; explicit allowlist only |

Each `static` entry has `kind` (`asset` or `page`), `sha256` (candidate bytes),
and `before_sha256` (current bytes, or JSON null for an absent path). Include all
changed files and their referenced new assets. Mark HTML and discovery files as
`page`; publish assets first. Existing assets cannot change bytes: give replacements
new names. Unlisted files and old assets are retained. For API-only releases,
include an unchanged page as a verified baseline; no implicit whole-directory copy.

The candidate effective Compose must equal the baseline in every field except
`services.api.image`, which must equal `new_image`. Preserve all environment,
mounts, credentials, loader and database settings. The current API's Compose
config hash must also match the baseline file. Obtain hashes with the same Compose
version/environment used for activation; a mismatch requires investigation, not
removal of the check. Relative bind paths must already normalize correctly.

On the release host, first validate (no service/static mutation), then activate
only after the separately authorized release review:

```sh
python3 deploy/release.py --plan "$RELEASE_PLAN"
python3 deploy/release.py --plan "$RELEASE_PLAN" --apply
```

Both calls acquire the same lock and repeat preflight. All checks run before API
activation or static publication. Private receipt/config/backup creation happens
only after preflight on `--apply`; no secrets are printed. Activation is exactly
`up -d --no-deps --no-build api`. Docker health, image ID, API environment/mounts,
and unrelated container membership/start times/status are checked. Static files
are replaced atomically, with assets first, and final bytes verified. Perform
public HTTP/browser checks separately; these host checks do not establish CDN or
external-registry correctness.

Failure restores changed pages and preexisting bytes, activates the immutable old
API image, verifies health and preservation, and writes `receipt.json`. New assets
remain available. A failed rollback is recorded explicitly. Receipts include
`plan.json`, private effective `compose.production.json`, `compose.rollback.json`,
and `before/`. Retain these and both images. Do not reuse a failed receipt directory
blindly. After a process kill/host failure, inspect actual state and the saved plan;
under the same shared lock, restore only the plan's changed pages from `before/`
(or remove only newly introduced pages), retain all assets, then invoke the saved
rollback Compose with the same project and `up -d --no-deps --no-build api`.
Recheck health, mounts, environment and static hashes. Never run a loader to recover
a code-only release. Individual file publication is atomic; a whole multi-page
release is not a single filesystem transaction.

Validate changes without a Docker daemon or production access:

```sh
python3 -m unittest discover -s tests -p test_release_entrypoint.py -v
```

Fixtures cover stale baselines, missing private inputs, Compose/runtime drift,
provenance, lock contention, path safety, asset retention, publication ordering,
and activation/publication rollback. These are simulated releases, not production
acceptance. Publish or activate this entry point only after its independent review.
