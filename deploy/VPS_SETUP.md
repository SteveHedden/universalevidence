# Deployment requirements and operations

This describes the existing Docker/Fuseki deployment layout for operators who already have the required data inputs. It is not a complete independent data-acquisition guide. The public checkout alone cannot initialize the full API.

## Before starting

- Provision the registry snapshots, geographic mirrors and crosswalks referenced by `scripts/load_fuseki.py` and `vocabularies/sources.ttl`, under their applicable [data terms](../DATA-LICENSING.md).
- The current Compose loader expects authorized R2 storage for automatic downloads. Credentials and access to the hosted service's storage are not supplied. Downloaded inputs overwrite matching local files; preserve local edits before running a configured loader.
- Use Docker with Compose v2, Python 3.11, Node.js 24 or newer, npm, and Bash for the guarded reload script. Verify installed versions; distribution package defaults may be older.
- Size the host for both containers and loading overhead. The current Compose limits allocate up to 3400 MB to Fuseki and 1500 MB to the API, before the loader and operating system.
- The included nginx and systemd files describe the UE host layout. Adapt domain names, certificate paths, directories and CORS origins for another installation. Do not expose Fuseki publicly.

The commands below assume an Ubuntu/Debian host with administrative access. Review paths and existing services before applying them.

## 1. Install dependencies

```bash
apt update && apt upgrade -y
apt install -y docker.io docker-compose-plugin git nginx nodejs npm certbot python3-certbot-nginx
systemctl enable docker
```

## 2. Clone repo and configure

```bash
cd /root
git clone https://github.com/SteveHedden/universalevidence.git
cd universalevidence
cp .env.example .env
nano .env   # configure authorized storage and a stable GRAPH_EDGE_TOKEN_SECRET
```

## 3. Configure emergency swap

The API is latency-sensitive, so swap is an emergency cushion rather than routine
memory. Create a 2 GiB swap file and use low swappiness:

```bash
fallocate -l 2G /swapfile
chmod 600 /swapfile
mkswap /swapfile
swapon /swapfile
grep -qF '/swapfile none swap sw 0 0' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
printf 'vm.swappiness=10\n' > /etc/sysctl.d/99-universalevidence-swap.conf
sysctl --system
```

Verify the active swap and setting:

```bash
free -h
swapon --show
sysctl vm.swappiness
```

## 4. Build the React frontend

```bash
cd site
npm ci
VITE_API_BASE_URL=/api npm run build
mkdir -p /var/www/universalevidence
cp -r dist/* /var/www/universalevidence/
cd ..
```

## 5. Start the stack (pulls data from R2 automatically)

```bash
docker compose up -d --wait
```

This starts Fuseki, runs the loader (downloads all data from R2), then starts the API. Loading time depends on the input size and host. Missing mappings can cause API startup to fail even if the loader exits successfully; inspect its logs and version manifest.

## 6. Configure nginx

```bash
cp deploy/nginx.conf /etc/nginx/sites-available/universalevidence
ln -s /etc/nginx/sites-available/universalevidence /etc/nginx/sites-enabled/
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx
```

## 7. TLS via Let's Encrypt (after DNS is pointed at this VPS)

```bash
certbot --nginx -d universalevidence.com -d www.universalevidence.com
```

## 8. Enable auto-start on reboot

```bash
cp deploy/universalevidence.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable universalevidence
```

## Updating the app

**Code-only updates**, after reviewing and recording the release and rollback image:

```bash
cd /root/universalevidence
git pull --ff-only
docker compose build api
docker compose up -d --no-deps api
docker compose ps -a
curl --fail http://127.0.0.1:8000/health
```

The image bakes in the source code. A restart alone does not update it. Verify the running image contains the expected release. `--no-deps` prevents a code-only API update from recreating Fuseki or rerunning the loader. Do not use this path when loader inputs or vocabulary versions change.

**Frontend updates:** run `npm ci` and `VITE_API_BASE_URL=/api npm run build` in `site/`. Back up the current index, publish new assets first, then replace `index.html` atomically on the same filesystem. Retain old hashed assets and license notices. Check the served index and its referenced assets. A frontend update does not require a database restart.

**Taxonomy or crosswalk updates** (after pushing taxonomy changes and uploading
crosswalk/mirror changes to R2):

First, from the release checkout containing the exact files uploaded to R2,
compute the content-addressed versions that production must reproduce:

```bash
python3 - <<'PY'
from scripts.load_fuseki import build_version_manifest
manifest = build_version_manifest()
if not manifest["taxonomyVersion"] or not manifest["datasetVersion"]:
    raise SystemExit(f"incomplete release inputs: {manifest}")
print("EXPECTED_TAXONOMY_VERSION=" + manifest["taxonomyVersion"])
print("EXPECTED_DATASET_VERSION=" + manifest["datasetVersion"])
PY
```

Then pass those two non-secret hashes explicitly on the VPS:

```bash
cd /root/universalevidence
EXPECTED_TAXONOMY_VERSION=<64-character taxonomy hash> \
EXPECTED_DATASET_VERSION=<64-character dataset hash> \
deploy/reload_taxonomy.sh
```

The production TDB2 volume must never be refreshed with `--taxonomy-only` or a
bare `docker compose run --rm loader`: both replace named graphs in place and
allow obsolete TDB2 index entries to accumulate. The reload script pulls the
latest git taxonomy, verifies R2 credentials, takes a checksummed paired backup
of both `fuseki-data` and `loader-manifests`, recreates both volumes, and checks
health, triple count, volume size, every loader input, the exact expected graph
versions, and all six required source/axis stamps. It automatically restores
both volumes if any post-reload verification fails. Allow at least 15 minutes
for the loader plus the backup/rebuild time. The default clean-volume ceiling is
4 GiB and can be tightened for a release with `FUSEKI_MAX_VOLUME_BYTES`.


## Verify and recover

After startup or an update, check API/Fuseki health, loader completion, expected version hashes and source/axis stamps. Then exercise taxonomy browsing, Search, Graph and study links. Inspect per-source metadata: an HTTP 200 response can still contain partial results. A local test without internet access does not validate live ClinicalTrials.gov or ISRCTN retrieval.

For code-only rollback, recreate the API using the saved prior image. For a frontend rollback, restore the previous index while retaining its referenced assets. For a data release, preserve and restore the paired Fuseki and loader-manifest backup using the guarded reload procedure; do not mix an old database with a new manifest.

The reload script assumes the existing R2-backed layout and corpus-specific validation thresholds. It is not a generic empty-database initializer for arbitrary datasets. Complete independent data provisioning is future work.

## Crawler discovery files

The frontend `prebuild` generates `robots.txt` and `sitemap.xml` from the current
States, Interventions, Regions and Sources Turtle files. Install Python 3 and
RDFLib in the build environment (for example `python3 -m venv .venv`, then
`.venv/bin/pip install rdflib`; activate that environment before `npm run build`).
Generation is offline and fails on missing/invalid vocabulary files or sitemap
size limits. The sitemap lists only supported public term pages plus the home,
ontology and tutorial pages; RDF-only listings, external term URIs, retired
concepts and query combinations are excluded. No fabricated last-modified dates
are emitted.

Publish both generated discovery files from `site/dist/` with every frontend
release. For vocabulary-only releases, regenerate after checking out the exact
release with `python3 scripts/generate_discovery.py --output /tmp/ue-discovery`
and atomically replace both files in `/var/www/universalevidence/` after the
vocabulary release succeeds. Retain their previous versions with the release
backup. Never generate from private construction copies of the vocabularies.

Install the exact `/robots.txt` and `/sitemap.xml` nginx locations in
`deploy/nginx.conf` (back up live configuration, run `nginx -t`, then reload).
Absent files return 404 rather than homepage HTML. Preserve other live nginx
settings. Do not disable Cloudflare managed robots policies: Cloudflare may
prepend its bot-specific restrictions to the origin's normal-search allowance.
Verify both apex and www responses through Cloudflare, check their content types,
parse the XML, and confirm that Malaria is included. Sitemap URLs always use
`https://universalevidence.com`, regardless of the request host.

Submit `https://universalevidence.com/sitemap.xml` in the site's Google Search
Console property, and inspect `https://universalevidence.com/vocab/states/Malaria`.
Record whether these steps were performed or handed off; neither guarantees indexing.
