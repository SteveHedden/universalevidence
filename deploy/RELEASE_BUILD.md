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
