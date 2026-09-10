#!/usr/bin/env bash
# Rebuild production Fuseki from canonical repo/R2 inputs without retaining TDB2 bloat.

set -Eeuo pipefail
# Never allow a caller's xtrace setting to print resolved Compose environment values.
set +x

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$ROOT"

BACKUP_DIR=${FUSEKI_BACKUP_DIR:-/root/fuseki-backups}
STATE_FILE=${TAXONOMY_RELOAD_STATE_FILE:-$ROOT/.taxonomy-reload-state.json}
INITIAL_TRIPLE_COUNT=1549501
# A legitimate clean corpus has previously exceeded 2 GiB. Keep a bounded,
# configurable ceiling with enough headroom for current crosswalk growth.
MAX_VOLUME_BYTES=${FUSEKI_MAX_VOLUME_BYTES:-$((4 * 1024 * 1024 * 1024))}
# The full loader can exceed the former six-minute bound on production data.
LOADER_TIMEOUT_SECONDS=${TAXONOMY_LOADER_TIMEOUT_SECONDS:-900}
EXPECTED_TAXONOMY_VERSION=${EXPECTED_TAXONOMY_VERSION:-}
EXPECTED_DATASET_VERSION=${EXPECTED_DATASET_VERSION:-}
CORE_TAXONOMY_SOURCES=(
  vocabularies/subjects.ttl
  vocabularies/states.ttl
  vocabularies/interventions.ttl
  vocabularies/regions.ttl
  vocabularies/sources.ttl
)

backup_bundle=
backup_staging_dir=
fuseki_backup_file=
manifest_backup_file=
fuseki_volume_name=
manifest_volume_name=
project_name=
rollback_armed=0
reload_complete=0
stack_stopped=0
actual_taxonomy_version=
actual_dataset_version=

log() { printf '[taxonomy-reload] %s\n' "$*"; }
die() { log "ERROR: $*" >&2; exit 1; }

require_uint_at_least() {
  local name=$1 value=$2 minimum=$3
  [[ $value =~ ^[0-9]+$ ]] || die "$name must be an integer; got '$value'"
  ((value >= minimum)) || die "$name must be at least $minimum; got $value"
}

require_sha256() {
  local name=$1 value=$2
  [[ $value =~ ^[0-9a-f]{64}$ ]] || die "$name must be an explicit lowercase SHA-256 value"
}

compose_project_name() {
  # --no-interpolate keeps secrets as placeholders. Only the non-secret project
  # name is emitted by the Python filter.
  docker compose config --no-interpolate --format json |
    python3 -c 'import json,sys; print(json.load(sys.stdin).get("name", ""))'
}

preflight() {
  command -v docker >/dev/null || die "docker is required"
  command -v git >/dev/null || die "git is required"
  command -v curl >/dev/null || die "curl is required"
  command -v python3 >/dev/null || die "python3 is required"
  command -v tar >/dev/null || die "tar is required"

  local source
  for source in "${CORE_TAXONOMY_SOURCES[@]}"; do
    [[ -f $source ]] || die "required taxonomy source is missing: $source"
  done

  require_uint_at_least FUSEKI_MAX_VOLUME_BYTES "$MAX_VOLUME_BYTES" 1
  require_uint_at_least TAXONOMY_LOADER_TIMEOUT_SECONDS "$LOADER_TIMEOUT_SECONDS" 900
  require_sha256 EXPECTED_TAXONOMY_VERSION "$EXPECTED_TAXONOMY_VERSION"
  require_sha256 EXPECTED_DATASET_VERSION "$EXPECTED_DATASET_VERSION"

  docker compose config --quiet || die "docker compose config failed"
  project_name=$(compose_project_name) || die "could not resolve Compose project name"
  [[ -n $project_name ]] || die "Compose project name is empty"

  mkdir -p "$BACKUP_DIR"
  [[ -w $BACKUP_DIR ]] || die "backup directory is not writable: $BACKUP_DIR"
  docker image inspect alpine:latest >/dev/null 2>&1 || docker pull alpine:latest >/dev/null
}

verify_loader_environment() {
  # Check only presence inside Compose's resolved environment. Do not print or
  # inspect credential values on the host.
  docker compose run --rm --no-deps loader python -c '
import os
missing = [
    name
    for name in ("R2_ENDPOINT_URL", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY")
    if not os.environ.get(name)
]
if missing:
    raise SystemExit("missing loader credentials: " + ", ".join(missing))
' || die "R2 credentials did not resolve inside the loader container"
}

resolve_volume_by_key() {
  local volume_key=$1
  local -a matches=()
  mapfile -t matches < <(docker volume ls \
    --filter "label=com.docker.compose.project=$project_name" \
    --filter "label=com.docker.compose.volume=$volume_key" \
    --format '{{.Name}}')
  ((${#matches[@]} == 1)) || die "expected exactly one '$volume_key' volume for project '$project_name'; found ${#matches[@]}"
  printf '%s\n' "${matches[0]}"
}

resolve_volumes() {
  fuseki_volume_name=$(resolve_volume_by_key fuseki-data)
  manifest_volume_name=$(resolve_volume_by_key loader-manifests)
}

volume_mountpoint() {
  docker volume inspect -f '{{.Mountpoint}}' "$1"
}

volume_size_bytes() {
  local mountpoint
  mountpoint=$(volume_mountpoint "$1")
  du -sb "$mountpoint" | awk '{print $1}'
}

archive_volume() {
  local volume=$1 target=$2
  docker run --rm -v "$volume:/data:ro" -v "$(dirname "$target"):/backup" alpine:latest \
    tar czf "/backup/$(basename "$target")" -C /data .
  [[ -s $target ]] || die "backup archive is empty: $target"
  tar -tzf "$target" >/dev/null || die "backup archive failed integrity check: $target"
}

write_backup_manifest() {
  python3 - "$backup_staging_dir" "$fuseki_volume_name" "$manifest_volume_name" <<'PY'
import datetime
import hashlib
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
volumes = {
    "fuseki-data": (sys.argv[2], root / "fuseki-data.tar.gz"),
    "loader-manifests": (sys.argv[3], root / "loader-manifests.tar.gz"),
}
payload = {
    "schemaVersion": "ue-taxonomy-backup-v1",
    "createdAt": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "volumes": {},
}

def file_sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

for key, (name, archive) in volumes.items():
    payload["volumes"][key] = {
        "name": name,
        "archive": archive.name,
        "size": archive.stat().st_size,
        "sha256": file_sha256(archive),
    }
(root / "backup-manifest.json").write_text(
    json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
)
PY
}

verify_backup_bundle() {
  python3 - "$backup_bundle" "$fuseki_volume_name" "$manifest_volume_name" <<'PY' || return 1
import hashlib
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
manifest = json.loads((root / "backup-manifest.json").read_text(encoding="utf-8"))
if manifest.get("schemaVersion") != "ue-taxonomy-backup-v1":
    raise SystemExit("invalid backup manifest schema")
expected_names = {"fuseki-data": sys.argv[2], "loader-manifests": sys.argv[3]}
if set(manifest.get("volumes", {})) != set(expected_names):
    raise SystemExit("backup manifest does not cover both required volumes")
def file_sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

for key, expected_name in expected_names.items():
    record = manifest["volumes"][key]
    if record.get("name") != expected_name:
        raise SystemExit(f"backup volume name mismatch for {key}")
    archive = root / record["archive"]
    if not archive.is_file() or archive.stat().st_size != record.get("size"):
        raise SystemExit(f"backup archive missing or wrong size for {key}")
    if file_sha256(archive) != record.get("sha256"):
        raise SystemExit(f"backup archive checksum mismatch for {key}")
PY
  tar -tzf "$backup_bundle/fuseki-data.tar.gz" >/dev/null || return 1
  tar -tzf "$backup_bundle/loader-manifests.tar.gz" >/dev/null || return 1
}

make_backup() {
  local timestamp available fuseki_bytes manifest_bytes required_bytes
  timestamp=$(date -u +%Y%m%dT%H%M%SZ)
  backup_bundle="$BACKUP_DIR/taxonomy-reload-$timestamp"
  [[ ! -e $backup_bundle ]] || die "backup target already exists: $backup_bundle"
  backup_staging_dir=$(mktemp -d "$BACKUP_DIR/.taxonomy-reload-$timestamp.XXXXXX")
  fuseki_backup_file="$backup_staging_dir/fuseki-data.tar.gz"
  manifest_backup_file="$backup_staging_dir/loader-manifests.tar.gz"

  fuseki_bytes=$(volume_size_bytes "$fuseki_volume_name")
  manifest_bytes=$(volume_size_bytes "$manifest_volume_name")
  required_bytes=$((fuseki_bytes + manifest_bytes))
  available=$(df -PB1 "$BACKUP_DIR" | awk 'NR==2 {print $4}')
  ((available > required_bytes)) || die "insufficient free space for backups (need >$required_bytes bytes, have $available)"

  log "Stopping stack while retaining both data volumes"
  docker compose down
  stack_stopped=1
  log "Archiving $fuseki_volume_name and $manifest_volume_name"
  archive_volume "$fuseki_volume_name" "$fuseki_backup_file"
  archive_volume "$manifest_volume_name" "$manifest_backup_file"
  write_backup_manifest

  # Publish the pair as one complete backup unit only after both archives and
  # their checksums exist. The stack remains down throughout the snapshot.
  mv "$backup_staging_dir" "$backup_bundle"
  backup_staging_dir=
  fuseki_backup_file="$backup_bundle/fuseki-data.tar.gz"
  manifest_backup_file="$backup_bundle/loader-manifests.tar.gz"
  verify_backup_bundle
  rollback_armed=1
}

container_health() {
  local service=$1 container
  container=$(docker compose ps -q "$service")
  [[ -n $container ]] || return 1
  [[ $(docker inspect -f '{{.State.Health.Status}}' "$container") == healthy ]]
}

loader_succeeded() {
  local container
  container=$(docker compose ps -q -a loader)
  [[ -n $container ]] || return 1
  [[ $(docker inspect -f '{{.State.ExitCode}}' "$container") == 0 ]]
}

wait_for_loader() {
  local container status exit_code deadline
  deadline=$((SECONDS + LOADER_TIMEOUT_SECONDS))
  while ((SECONDS < deadline)); do
    container=$(docker compose ps -q -a loader)
    if [[ -n $container ]]; then
      status=$(docker inspect -f '{{.State.Status}}' "$container")
      if [[ $status == exited ]]; then
        exit_code=$(docker inspect -f '{{.State.ExitCode}}' "$container")
        [[ $exit_code == 0 ]] || die "loader exited with status $exit_code"
        return 0
      fi
    fi
    sleep 2
  done
  die "loader did not finish within $LOADER_TIMEOUT_SECONDS seconds"
}

wait_for_health() {
  local service=$1
  for _ in $(seq 1 90); do
    container_health "$service" && return 0
    sleep 2
  done
  die "$service did not become healthy within 3 minutes"
}

start_fresh_stack() {
  # Images are built before downtime. Starting API in the same invocation can
  # wedge on older Compose versions after the one-shot loader exits.
  docker compose up -d fuseki loader
  wait_for_health fuseki
  wait_for_loader
  docker compose up -d --no-deps api
  wait_for_health api
}

triple_count() {
  curl -fsS --get \
    --data-urlencode 'query=SELECT (COUNT(*) AS ?count) WHERE { GRAPH ?g { ?s ?p ?o } }' \
    -H 'Accept: application/sparql-results+json' \
    http://127.0.0.1:3030/ue/query |
    python3 -c 'import json,sys; print(json.load(sys.stdin)["results"]["bindings"][0]["count"]["value"])'
}

last_good_count() {
  if [[ -f $STATE_FILE ]]; then
    python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["last_triple_count"])' "$STATE_FILE"
  else
    printf '%s\n' "$INITIAL_TRIPLE_COUNT"
  fi
}

verify_version_manifest() {
  local manifest_path versions
  manifest_path=${TAXONOMY_RELOAD_MANIFEST_PATH:-/manifests/graph-versions.json}
  # Run this check inside the release loader image. That guarantees use of the
  # exact load_fuseki implementation and dependencies that authored the
  # manifest, without requiring rdflib or application packages on the host.
  versions=$(docker compose run --rm --no-deps \
    -e "UE_GRAPH_VERSION_MANIFEST=$manifest_path" \
    -e "EXPECTED_TAXONOMY_VERSION=$EXPECTED_TAXONOMY_VERSION" \
    -e "EXPECTED_DATASET_VERSION=$EXPECTED_DATASET_VERSION" \
    loader python - <<'PY'
import json
import os
import pathlib
import re
from scripts import load_fuseki

manifest_path = pathlib.Path(os.environ["UE_GRAPH_VERSION_MANIFEST"])
expected_taxonomy_version = os.environ["EXPECTED_TAXONOMY_VERSION"]
expected_dataset_version = os.environ["EXPECTED_DATASET_VERSION"]
if not manifest_path.is_file():
    raise SystemExit(f"loader version manifest is missing: {manifest_path}")

payload = json.loads(manifest_path.read_text(encoding="utf-8"))
if payload.get("schemaVersion") != "ue-loader-versions-v1":
    raise SystemExit("unexpected loader manifest schema")

def relative(path):
    return path.resolve().relative_to(load_fuseki.REPO_ROOT.resolve()).as_posix()

expected_paths = {
    "taxonomy": {relative(path) for path in load_fuseki.DEFAULT_TAXONOMY_SOURCES},
    "dataset": {relative(path) for path in (load_fuseki.AEA_SOURCE, load_fuseki.WHO_ICTRP_SOURCE)},
}

for section in ("taxonomy", "dataset"):
    detail = payload.get(section)
    if not isinstance(detail, dict):
        raise SystemExit(f"loader manifest lacks {section} detail")
    if detail.get("missing") != []:
        raise SystemExit(f"loader manifest reports missing {section} inputs: {detail.get('missing')!r}")
    records = detail.get("inputs")
    if not isinstance(records, list):
        raise SystemExit(f"loader manifest has invalid {section} inputs")
    paths = [record.get("path") for record in records]
    if len(paths) != len(set(paths)):
        raise SystemExit(f"loader manifest repeats a {section} input path")
    if set(paths) != expected_paths[section]:
        missing = sorted(expected_paths[section] - set(paths))
        extra = sorted(set(paths) - expected_paths[section])
        raise SystemExit(f"loader manifest {section} inputs differ; missing={missing}, extra={extra}")
    for record in records:
        if not isinstance(record.get("size"), int) or record["size"] <= 0:
            raise SystemExit(f"invalid size for loader input {record.get('path')!r}")
        if not re.fullmatch(r"[0-9a-f]{64}", str(record.get("sha256") or "")):
            raise SystemExit(f"invalid SHA-256 for loader input {record.get('path')!r}")
    declared = payload.get(f"{section}Version")
    if not re.fullmatch(r"[0-9a-f]{64}", str(declared or "")):
        raise SystemExit(f"{section}Version is null or invalid")
    # Call the loader's implementation directly so release verification cannot
    # drift from the code that authored the manifest.
    computed = load_fuseki._manifest_version(records)
    if declared != computed:
        raise SystemExit(f"{section}Version does not match its exact input records")

if payload["taxonomyVersion"] != expected_taxonomy_version:
    raise SystemExit(
        f"taxonomyVersion mismatch: got {payload['taxonomyVersion']}, expected {expected_taxonomy_version}"
    )
if payload["datasetVersion"] != expected_dataset_version:
    raise SystemExit(
        f"datasetVersion mismatch: got {payload['datasetVersion']}, expected {expected_dataset_version}"
    )
print(payload["taxonomyVersion"], payload["datasetVersion"])
PY
) || die "loader version manifest verification failed"
  read -r actual_taxonomy_version actual_dataset_version <<<"$versions"
}

verify_required_stamps() {
  local response summary
  response=$(curl -fsS --get \
    --data-urlencode 'query=PREFIX ue: <https://universalevidence.com/ontology/>
SELECT ?graph ?predicate (COUNT(*) AS ?count) WHERE {
  VALUES (?graph ?predicate) {
    (<https://universalevidence.com/graph/aea> ue:matchesCondition)
    (<https://universalevidence.com/graph/aea> ue:matchesIntervention)
    (<https://universalevidence.com/graph/aea> ue:matchesOutcome)
    (<https://universalevidence.com/graph/who-ictrp> ue:matchesCondition)
    (<https://universalevidence.com/graph/who-ictrp> ue:matchesIntervention)
    (<https://universalevidence.com/graph/who-ictrp> ue:matchesOutcome)
  }
  GRAPH ?graph { ?study ?predicate ?concept }
}
GROUP BY ?graph ?predicate' \
    -H 'Accept: application/sparql-results+json' \
    http://127.0.0.1:3030/ue/query) || die "could not query required concept stamps"

  summary=$(python3 - "$response" <<'PY'
import json
import sys

payload = json.loads(sys.argv[1])
expected = {
    ("https://universalevidence.com/graph/aea", "https://universalevidence.com/ontology/matchesCondition"),
    ("https://universalevidence.com/graph/aea", "https://universalevidence.com/ontology/matchesIntervention"),
    ("https://universalevidence.com/graph/aea", "https://universalevidence.com/ontology/matchesOutcome"),
    ("https://universalevidence.com/graph/who-ictrp", "https://universalevidence.com/ontology/matchesCondition"),
    ("https://universalevidence.com/graph/who-ictrp", "https://universalevidence.com/ontology/matchesIntervention"),
    ("https://universalevidence.com/graph/who-ictrp", "https://universalevidence.com/ontology/matchesOutcome"),
}
counts = {}
for row in payload.get("results", {}).get("bindings", []):
    key = (row["graph"]["value"], row["predicate"]["value"])
    counts[key] = int(row["count"]["value"])
missing = sorted(expected - set(counts))
empty = sorted(key for key in expected if counts.get(key, 0) <= 0)
extra = sorted(set(counts) - expected)
if missing or empty or extra:
    raise SystemExit(f"required stamp verification failed: missing={missing}, empty={empty}, extra={extra}")
print(", ".join(f"{graph.rsplit('/', 1)[-1]}:{predicate.rsplit('/', 1)[-1]}={counts[(graph, predicate)]}" for graph, predicate in sorted(expected)))
PY
) || die "required concept stamp verification failed"
  log "Verified required concept stamps: $summary"
}

write_state() {
  local count=$1 tmp="$STATE_FILE.tmp"
  python3 - "$count" "$actual_taxonomy_version" "$actual_dataset_version" "$tmp" <<'PY'
import datetime, json, sys
payload = {
    "last_triple_count": int(sys.argv[1]),
    "taxonomy_version": sys.argv[2],
    "dataset_version": sys.argv[3],
    "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
}
with open(sys.argv[4], "w", encoding="utf-8") as fh:
    json.dump(payload, fh, indent=2)
    fh.write("\n")
PY
  mv "$tmp" "$STATE_FILE"
}

verify_reload() {
  loader_succeeded || die "loader did not exit successfully"
  container_health fuseki || die "Fuseki is not healthy"
  container_health api || die "API is not healthy"

  local baseline floor count bytes
  baseline=$(last_good_count)
  floor=$((baseline * 90 / 100))
  ((floor >= 1390000)) || floor=1390000
  count=$(triple_count)
  ((count >= floor)) || die "triple count $count is below required floor $floor"

  resolve_volumes
  bytes=$(volume_size_bytes "$fuseki_volume_name")
  ((bytes < MAX_VOLUME_BYTES)) || die "Fuseki volume is $bytes bytes; expected under $MAX_VOLUME_BYTES"
  verify_version_manifest
  verify_required_stamps
  [[ ${TAXONOMY_RELOAD_TEST_FAIL:-0} != 1 ]] || die "simulated post-reload verification failure"

  write_state "$count"
  log "Verified: triples=$count (floor=$floor), fuseki_volume_bytes=$bytes, taxonomy_version=$actual_taxonomy_version, dataset_version=$actual_dataset_version"
}

create_compose_volume() {
  local volume_name=$1 volume_key=$2
  docker volume create \
    --label "com.docker.compose.project=$project_name" \
    --label "com.docker.compose.volume=$volume_key" \
    "$volume_name" >/dev/null
}

restore_volume() {
  local volume_name=$1 archive=$2
  docker run --rm -v "$volume_name:/data" -v "$backup_bundle:/backup:ro" alpine:latest \
    tar xzf "/backup/$(basename "$archive")" -C /data
}

rollback() {
  local status=$1
  trap - EXIT ERR INT TERM
  log "Reload failed; restoring both data volumes from $backup_bundle"
  verify_backup_bundle || {
    log "ERROR: backup bundle failed integrity verification; refusing destructive rollback" >&2
    exit "$status"
  }
  docker compose down || exit "$status"
  docker volume rm -f "$fuseki_volume_name" "$manifest_volume_name" >/dev/null || exit "$status"
  create_compose_volume "$fuseki_volume_name" fuseki-data
  create_compose_volume "$manifest_volume_name" loader-manifests
  restore_volume "$fuseki_volume_name" "$fuseki_backup_file"
  restore_volume "$manifest_volume_name" "$manifest_backup_file"

  # Bypass the one-shot loader so bad current input cannot mutate the restored store.
  docker compose up -d --no-deps fuseki
  for _ in $(seq 1 60); do container_health fuseki && break; sleep 2; done
  docker compose up -d --no-deps api
  for _ in $(seq 1 60); do container_health api && break; sleep 2; done
  if container_health fuseki && container_health api; then
    log "Rollback restored both volumes and service from $backup_bundle"
  else
    log "ERROR: rollback restored both volumes but services are not healthy" >&2
  fi
  exit "$status"
}

on_exit() {
  local status=$?
  if [[ -n $backup_staging_dir && -d $backup_staging_dir ]]; then
    rm -rf -- "$backup_staging_dir"
  fi
  if ((status != 0 && rollback_armed == 1 && reload_complete == 0)); then
    rollback "$status"
  elif ((status != 0 && stack_stopped == 1 && reload_complete == 0)); then
    log "Failure occurred before volume deletion; restarting the existing stack"
    docker compose up -d --wait || true
  fi
}

main() {
  trap on_exit EXIT

  preflight
  log "Updating checkout before maintenance"
  git pull --ff-only
  log "Building release images before downtime"
  docker compose build fuseki loader api
  verify_loader_environment
  resolve_volumes
  make_backup

  log "Removing old Fuseki and loader-manifest volumes"
  docker volume rm "$fuseki_volume_name" "$manifest_volume_name"
  log "Rebuilding and loading a fresh stack"
  start_fresh_stack
  verify_reload

  reload_complete=1
  rollback_armed=0
  stack_stopped=0
  log "Taxonomy reload completed successfully; paired backup retained at $backup_bundle"
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
  main "$@"
fi
