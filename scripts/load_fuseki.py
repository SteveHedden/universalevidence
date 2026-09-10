#!/usr/bin/env python3
"""Load Universal Evidence Turtle files into Fuseki named graphs."""

import argparse
import hashlib
import json
import logging
import os
from pathlib import Path
import tempfile
from typing import Any, Iterable
from urllib.parse import urlencode

import requests
from rdflib import Graph
from rdflib.namespace import SKOS

logger = logging.getLogger(__name__)


class ConceptStampingError(RuntimeError):
    """Raised when any required study-to-concept stamp cannot be rebuilt."""


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_GRAPH_STORE_URL = "http://localhost:3030/ue/data"
DEFAULT_VERSION_MANIFEST = REPO_ROOT / "data" / "manifests" / "graph-versions.json"
# ``requests`` uses the connect timeout while it is still writing a request
# body.  The merged production taxonomy is large enough that Fuseki can apply
# backpressure for longer than 30 seconds, even though both containers remain
# healthy.  Keep the upload bounded, but give Graph Store PUTs enough time to
# stream and commit the full payload.
GRAPH_STORE_UPLOAD_TIMEOUT_SECONDS = float(
    os.getenv("FUSEKI_GRAPH_STORE_UPLOAD_TIMEOUT_SECONDS", "600")
)

R2_PREFIXES = [
    ("raw/", REPO_ROOT / "data" / "raw"),
    ("crosswalks/", REPO_ROOT / "vocabularies" / "crosswalks"),
    ("mirrors/", REPO_ROOT / "vocabularies" / "mirrors"),
]
AEA_GRAPH_URI = "https://universalevidence.com/graph/aea"
TAXONOMY_GRAPH_URI = "https://universalevidence.com/graph/ue-taxonomy"
WHO_ICTRP_GRAPH_URI = "https://universalevidence.com/graph/who-ictrp"
AEA_SOURCE       = REPO_ROOT / "data" / "raw" / "aea-rct-raw.ttl"
WHO_ICTRP_SOURCE = REPO_ROOT / "data" / "raw" / "who-ictrp-raw.ttl"
DEFAULT_TAXONOMY_SOURCES = (
    # Core thesauri only — mirrors are engine construction inputs, not query-time vocab
    REPO_ROOT / "vocabularies" / "subjects.ttl",
    REPO_ROOT / "vocabularies" / "states.ttl",
    REPO_ROOT / "vocabularies" / "interventions.ttl",
    REPO_ROOT / "vocabularies" / "regions.ttl",
    REPO_ROOT / "vocabularies" / "mirrors" / "geonames-countries-mirror.ttl",
    REPO_ROOT / "vocabularies" / "mirrors" / "geonames-adm1-mirror.ttl",
    REPO_ROOT / "vocabularies" / "sources.ttl",
    REPO_ROOT / "vocabularies" / "crosswalks" / "aea-conditions-crosswalk.ttl",
    REPO_ROOT / "vocabularies" / "crosswalks" / "aea-interventions-crosswalk.ttl",
    REPO_ROOT / "vocabularies" / "crosswalks" / "aea-interventions-freetext-crosswalk.ttl",
    REPO_ROOT / "vocabularies" / "crosswalks" / "aea-regions-crosswalk.ttl",
    REPO_ROOT / "vocabularies" / "crosswalks" / "who-ictrp-conditions-crosswalk.ttl",
    REPO_ROOT / "vocabularies" / "crosswalks" / "who-ictrp-interventions-crosswalk.ttl",
    REPO_ROOT / "vocabularies" / "crosswalks" / "who-ictrp-outcomes-crosswalk.ttl",
    REPO_ROOT / "vocabularies" / "crosswalks" / "who-ictrp-regions-crosswalk.ttl",
    REPO_ROOT / "vocabularies" / "crosswalks" / "ctgov-interventions-crosswalk.ttl",
    REPO_ROOT / "vocabularies" / "crosswalks" / "ctgov-outcomes-crosswalk.ttl",
    REPO_ROOT / "vocabularies" / "crosswalks" / "ctgov-regions-crosswalk.ttl",
    REPO_ROOT / "vocabularies" / "crosswalks" / "isrctn-states-crosswalk.ttl",
    REPO_ROOT / "vocabularies" / "crosswalks" / "isrctn-interventions-crosswalk.ttl",
    REPO_ROOT / "vocabularies" / "crosswalks" / "isrctn-outcomes-crosswalk.ttl",
    REPO_ROOT / "vocabularies" / "crosswalks" / "isrctn-regions-crosswalk.ttl",
    REPO_ROOT / "vocabularies" / "crosswalks" / "aea-outcomes-crosswalk.ttl",
)


def _input_record(path: Path) -> dict[str, Any]:
    """Describe one exact loader input without depending on file timestamps."""
    resolved = path.resolve()
    try:
        relative = resolved.relative_to(REPO_ROOT.resolve()).as_posix()
    except ValueError:
        relative = str(resolved)
    payload = path.read_bytes()
    return {
        "path": relative,
        "size": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def _manifest_version(records: Iterable[dict[str, Any]]) -> str:
    canonical = json.dumps(
        sorted(records, key=lambda record: str(record.get("path") or "")),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def build_version_manifest(
    *,
    taxonomy_sources: Iterable[Path] = DEFAULT_TAXONOMY_SOURCES,
    dataset_sources: Iterable[Path] = (AEA_SOURCE, WHO_ICTRP_SOURCE),
    dataset_records: Iterable[dict[str, Any]] | None = None,
    dataset_missing: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Build stable versions from the exact taxonomy and source snapshots.

    Missing inputs make that side unavailable instead of inventing a process-
    local version. Graph v2 will consequently bypass its cache.
    """
    taxonomy_paths = tuple(taxonomy_sources)
    dataset_paths = tuple(dataset_sources)
    taxonomy_missing = [str(path) for path in taxonomy_paths if not path.is_file()]
    missing_dataset = (
        list(dataset_missing)
        if dataset_missing is not None
        else [str(path) for path in dataset_paths if not path.is_file()]
    )
    taxonomy_inputs = [_input_record(path) for path in taxonomy_paths if path.is_file()]
    dataset_inputs = (
        list(dataset_records)
        if dataset_records is not None
        else [_input_record(path) for path in dataset_paths if path.is_file()]
    )
    return {
        "schemaVersion": "ue-loader-versions-v1",
        "taxonomyVersion": (
            _manifest_version(taxonomy_inputs) if not taxonomy_missing else None
        ),
        "datasetVersion": (
            _manifest_version(dataset_inputs) if dataset_inputs and not missing_dataset else None
        ),
        "taxonomy": {
            "inputs": taxonomy_inputs,
            "missing": taxonomy_missing,
        },
        "dataset": {
            "inputs": dataset_inputs,
            "missing": missing_dataset,
        },
    }


def read_version_manifest(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if payload.get("schemaVersion") == "ue-loader-versions-v1" else None


def write_version_manifest(path: Path, manifest: dict[str, Any]) -> None:
    """Atomically publish loader versions only after the load has completed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    ) as handle:
        json.dump(manifest, handle, indent=2, sort_keys=True)
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.replace(path)
    logger.info(
        "Wrote graph version manifest %s taxonomy=%s dataset=%s",
        path,
        manifest.get("taxonomyVersion") or "unavailable",
        manifest.get("datasetVersion") or "unavailable",
    )


def unpublish_version_manifest(path: Path) -> None:
    """Remove a previously published version before mutating its graphs.

    A loader failure after the graph upload starts must never leave an older
    manifest advertising versions that no longer describe the live graphs.
    """
    try:
        path.unlink()
    except FileNotFoundError:
        return
    logger.info("Unpublished graph version manifest before loading: %s", path)


def sync_from_r2(taxonomy_only: bool = False) -> None:
    """Pull files from Cloudflare R2.

    Reads R2_ENDPOINT_URL, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, and
    R2_BUCKET from the environment. Skips silently if any are absent (local
    dev with files already present). If taxonomy_only, skips the raw/ prefix
    (study data) and only downloads crosswalks and mirrors.
    """
    endpoint = os.environ.get("R2_ENDPOINT_URL")
    access_key = os.environ.get("R2_ACCESS_KEY_ID")
    secret_key = os.environ.get("R2_SECRET_ACCESS_KEY")
    bucket = os.environ.get("R2_BUCKET", "universalevidence-data")

    if not all([endpoint, access_key, secret_key]):
        logger.info("R2 credentials not set — using local files only")
        return

    try:
        import boto3
    except ImportError:
        logger.warning("boto3 not installed — skipping R2 sync")
        return

    s3 = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name="auto",
    )

    prefixes = [(p, d) for p, d in R2_PREFIXES if not (taxonomy_only and p == "raw/")]
    for prefix, local_dir in prefixes:
        local_dir.mkdir(parents=True, exist_ok=True)
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                filename = Path(key).name
                local_path = local_dir / filename
                logger.info("Downloading s3://%s/%s -> %s", bucket, key, local_path)
                s3.download_file(bucket, key, str(local_path))


def graph_store_url(base_url: str, graph_uri: str) -> str:
    """Return a Graph Store URL for a specific named graph."""
    separator = "&" if "?" in base_url else "?"
    return f"{base_url}{separator}{urlencode({'graph': graph_uri})}"


def validate_turtle(path: Path) -> None:
    """Parse a Turtle file before upload so loader failures are local and clear."""
    Graph().parse(path, format="turtle")


def add_narrower_transitive(graph: Graph) -> None:
    """Materialize skos:narrowerTransitive for all concept pairs in the graph.

    Computes the full transitive closure of skos:broader (inverted) so that
    SPARQL queries can use a single indexed hop instead of a recursive path
    at query time.
    """
    child_map: dict = {}
    for child, _, parent in graph.triples((None, SKOS.broader, None)):
        child_map.setdefault(parent, set()).add(child)

    def descendants(concept):
        visited, queue = set(), list(child_map.get(concept, set()))
        while queue:
            c = queue.pop()
            if c not in visited:
                visited.add(c)
                queue.extend(child_map.get(c, set()))
        return visited

    count = 0
    for parent in list(child_map):
        for desc in descendants(parent):
            graph.add((parent, SKOS.narrowerTransitive, desc))
            count += 1
    logger.info("Materialized %d skos:narrowerTransitive triples", count)


def merge_turtle_sources(paths: tuple[Path, ...]) -> bytes:
    """Parse and serialize multiple Turtle files as one graph payload."""
    graph = Graph()
    for path in paths:
        if not path.is_file():
            logger.warning("Mirror not found, skipping: %s", path)
            continue
        logger.info("Parsing %s", path)
        graph.parse(path, format="turtle")

    add_narrower_transitive(graph)
    return graph.serialize(format="turtle").encode("utf-8")


def put_named_graph(path: Path, graph_uri: str, graph_store_base: str) -> None:
    """Replace one Fuseki named graph with one Turtle file."""
    if not path.is_file():
        raise FileNotFoundError(f"Missing RDF input file: {path}")

    validate_turtle(path)
    target_url = graph_store_url(graph_store_base, graph_uri)
    logger.info("PUT %s -> %s", path, graph_uri)
    with open(path, "rb") as fh:
        response = requests.put(
            target_url,
            data=fh,
            headers={"Content-Type": "text/turtle"},
            timeout=GRAPH_STORE_UPLOAD_TIMEOUT_SECONDS,
        )
    response.raise_for_status()


def put_named_graph_payload(payload: bytes, graph_uri: str, graph_store_base: str) -> None:
    """Replace one Fuseki named graph with a prepared Turtle payload."""
    target_url = graph_store_url(graph_store_base, graph_uri)
    logger.info("PUT merged payload -> %s", graph_uri)
    response = requests.put(
        target_url,
        data=payload,
        headers={"Content-Type": "text/turtle"},
        timeout=GRAPH_STORE_UPLOAD_TIMEOUT_SECONDS,
    )
    response.raise_for_status()


def _select_pairs(query_url: str, select_query: str) -> list[tuple[str, str]]:
    """Run a SELECT ?study ?ancestor query and return (study_uri, ancestor_uri) pairs."""
    resp = requests.get(
        query_url,
        params={"query": select_query},
        headers={"Accept": "application/sparql-results+json"},
        timeout=(30, None),
    )
    resp.raise_for_status()
    bindings = resp.json()["results"]["bindings"]
    return [(b["study"]["value"], b["ancestor"]["value"]) for b in bindings]


def _insert_data_batched(
    update_url: str,
    graph_uri: str,
    predicate_uri: str,
    pairs: list[tuple[str, str]],
    batch_size: int = 2000,
) -> int:
    """Insert (subject, predicate, object) triples into a named graph in batches."""
    total = 0
    for i in range(0, len(pairs), batch_size):
        batch = pairs[i : i + batch_size]
        triples = " ".join(f"<{s}> <{predicate_uri}> <{o}> ." for s, o in batch)
        update = f"INSERT DATA {{ GRAPH <{graph_uri}> {{ {triples} }} }}"
        resp = requests.post(
            update_url,
            data={"update": update},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=(30, None),
        )
        resp.raise_for_status()
        total += len(batch)
    return total


def _clear_stamp_predicate(update_url: str, graph_uri: str, predicate_uri: str) -> None:
    """Remove all existing stamp triples for one predicate from a graph."""
    update = f"DELETE WHERE {{ GRAPH <{graph_uri}> {{ ?s <{predicate_uri}> ?o }} }}"
    resp = requests.post(
        update_url,
        data={"update": update},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=(30, None),
    )
    resp.raise_for_status()


def stamp_concept_matches(graph_store_base: str = DEFAULT_GRAPH_STORE_URL) -> None:
    """Pre-compute study→concept match triples (including all ancestors) at load time.

    TDB2 does not support cross-named-graph joins inside SPARQL UPDATE WHERE clauses.
    Workaround: run as SELECT (which does support cross-graph joins), then INSERT DATA
    the results in batches. Stale stamps are cleared before re-inserting.
    """
    query_url = graph_store_base.rsplit("/", 1)[0] + "/sparql"
    update_url = graph_store_base.rsplit("/", 1)[0] + "/update"
    UE      = "https://universalevidence.com/ontology/"
    AEA_NS  = "https://socialscienceregistry.org/schema#"
    ICTRP   = "https://universalevidence.com/source/who-ictrp/schema#"
    SKOS    = "http://www.w3.org/2004/02/skos/core#"
    TG      = TAXONOMY_GRAPH_URI

    stamps = [
        # (label, target_graph, predicate, SELECT query)
        ("AEA matchesCondition", AEA_GRAPH_URI, f"{UE}matchesCondition",
         f"""PREFIX ue: <{UE}> PREFIX aea: <{AEA_NS}> PREFIX skos: <{SKOS}>
SELECT DISTINCT ?study ?ancestor WHERE {{
  GRAPH <{AEA_GRAPH_URI}> {{ ?study aea:keyword ?kw . }}
  GRAPH <{TG}> {{
    ?entry ue:rawText ?kw ; (skos:exactMatch|skos:closeMatch) ?concept .
    ?concept a ue:State .
    ?ancestor skos:narrowerTransitive? ?concept .
  }}
}}"""),
        # UNION of two independent signals: the researcher-assigned
        # aea:keyword tag (existing, via aea-interventions-crosswalk.ttl) and
        # the free-text aea:intervention description (new, via
        # aea-interventions-freetext-crosswalk.ttl -- built by
        # scripts/extract_aea_intervention_freetext.py, which already applies
        # a specificity-safe upgrade rule so its auto-written entries never
        # contradict an existing keyword-derived match for the same study).
        # Both branches can fire for the same study; matchesIntervention
        # already supports multiple stamps per study (all_stamped/ancestor
        # expansion), so no dedup logic is needed beyond DISTINCT.
        ("AEA matchesIntervention", AEA_GRAPH_URI, f"{UE}matchesIntervention",
         f"""PREFIX ue: <{UE}> PREFIX aea: <{AEA_NS}> PREFIX skos: <{SKOS}>
SELECT DISTINCT ?study ?ancestor WHERE {{
  {{
    GRAPH <{AEA_GRAPH_URI}> {{ ?study aea:keyword ?kw . }}
    GRAPH <{TG}> {{
      ?entry ue:rawText ?kw ; (skos:exactMatch|skos:closeMatch) ?concept .
      ?concept a ue:Intervention .
    }}
  }} UNION {{
    GRAPH <{AEA_GRAPH_URI}> {{ ?study aea:intervention ?itextRaw . }}
    # Older producer entries collapse whitespace, while reviewed
    # recovery entries preserve the exact source literal (including CR/LF).
    # Keep both indexed joins so either durable representation stamps the study.
    BIND(REPLACE(REPLACE(?itextRaw, "^\\\\s+|\\\\s+$", ""), "\\\\s+", " ") AS ?itextNormalized)
    GRAPH <{TG}> {{
      {{
        ?entry ue:rawText ?itextRaw ; (skos:exactMatch|skos:closeMatch) ?concept .
      }} UNION {{
        ?entry ue:rawText ?itextNormalized ; (skos:exactMatch|skos:closeMatch) ?concept .
      }}
      ?concept a ue:Intervention .
    }}
  }}
  GRAPH <{TG}> {{ ?ancestor skos:narrowerTransitive? ?concept . }}
}}"""),
        ("WHO ICTRP matchesCondition", WHO_ICTRP_GRAPH_URI, f"{UE}matchesCondition",
         f"""PREFIX ue: <{UE}> PREFIX ictrp: <{ICTRP}> PREFIX skos: <{SKOS}>
SELECT DISTINCT ?study ?ancestor WHERE {{
  GRAPH <{WHO_ICTRP_GRAPH_URI}> {{ ?study ictrp:condition ?kw . }}
  GRAPH <{TG}> {{
    ?entry ue:rawText ?kw ; (skos:exactMatch|skos:closeMatch) ?concept .
    ?ancestor skos:narrowerTransitive? ?concept .
  }}
}}"""),
        ("WHO ICTRP matchesIntervention", WHO_ICTRP_GRAPH_URI, f"{UE}matchesIntervention",
         f"""PREFIX ue: <{UE}> PREFIX ictrp: <{ICTRP}> PREFIX skos: <{SKOS}>
SELECT DISTINCT ?study ?ancestor WHERE {{
  GRAPH <{WHO_ICTRP_GRAPH_URI}> {{ ?study ictrp:intervention ?kw . }}
  GRAPH <{TG}> {{
    ?entry ue:rawText ?kw ; (skos:exactMatch|skos:closeMatch) ?concept .
    ?ancestor skos:narrowerTransitive? ?concept .
  }}
}}"""),
        ("AEA matchesOutcome", AEA_GRAPH_URI, f"{UE}matchesOutcome",
         f"""PREFIX ue: <{UE}> PREFIX aea: <{AEA_NS}> PREFIX skos: <{SKOS}>
SELECT DISTINCT ?study ?ancestor WHERE {{
  GRAPH <{AEA_GRAPH_URI}> {{ ?study aea:primaryOutcome ?kw . }}
  GRAPH <{TG}> {{
    ?entry ue:rawText ?kw ; (skos:exactMatch|skos:closeMatch) ?concept .
    ?concept a ue:State .
    ?ancestor skos:narrowerTransitive? ?concept .
  }}
}}"""),
        ("WHO ICTRP matchesOutcome", WHO_ICTRP_GRAPH_URI, f"{UE}matchesOutcome",
         f"""PREFIX ue: <{UE}> PREFIX ictrp: <{ICTRP}> PREFIX skos: <{SKOS}>
SELECT DISTINCT ?study ?ancestor WHERE {{
  GRAPH <{WHO_ICTRP_GRAPH_URI}> {{
    {{ ?study ictrp:primaryOutcome ?kw . }} UNION {{ ?study ictrp:secondaryOutcome ?kw . }}
  }}
  GRAPH <{TG}> {{
    ?entry ue:rawText ?kw ; (skos:exactMatch|skos:closeMatch) ?concept .
    ?ancestor skos:narrowerTransitive? ?concept .
  }}
}}"""),
    ]

    failures: list[str] = []
    for label, graph_uri, predicate_uri, select_query in stamps:
        logger.info("Stamping: %s ...", label)
        try:
            _clear_stamp_predicate(update_url, graph_uri, predicate_uri)
            pairs = _select_pairs(query_url, select_query)
            if pairs:
                n = _insert_data_batched(update_url, graph_uri, predicate_uri, pairs)
                logger.info("Stamp OK: %s — %d triples", label, n)
            else:
                logger.info("Stamp OK: %s — 0 triples (graph absent or no matches)", label)
        except Exception:
            failures.append(label)
            logger.error("Stamp failed for %s", label, exc_info=True)
    if failures:
        raise ConceptStampingError(
            "concept match stamping failed: " + ", ".join(failures)
        )
    logger.info("Concept match stamping complete.")


def graph_has_triples(graph_uri: str, graph_store_base: str) -> bool:
    """Return True if a named graph already contains at least one triple."""
    query_url = graph_store_base.rsplit("/", 1)[0] + "/sparql"
    resp = requests.get(
        query_url,
        params={"query": f"ASK {{ GRAPH <{graph_uri}> {{ ?s ?p ?o }} }}"},
        headers={"Accept": "application/sparql-results+json"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json().get("boolean", False)


def load_default_graphs(
    graph_store_base: str = DEFAULT_GRAPH_STORE_URL,
    taxonomy_only: bool = False,
    skip_existing_raw: bool = False,
    version_manifest_path: Path | None = None,
) -> None:
    """Load RDF graphs into Fuseki. If taxonomy_only, skips raw study graphs.
    If skip_existing_raw, a populated raw graph is skipped only when a prior
    loader manifest proves its snapshot; otherwise it is safely reloaded."""
    prior_manifest = (
        read_version_manifest(version_manifest_path)
        if version_manifest_path is not None
        else None
    )
    prior_dataset_records = {
        str(record.get("path") or ""): record
        for record in ((prior_manifest or {}).get("dataset") or {}).get("inputs", ())
    }
    if version_manifest_path is not None:
        # From this point forward the graph may diverge from the previously
        # published identity. Keep Graph v2 in cache-bypass mode until every
        # upload and required concept-stamping pass has succeeded.
        unpublish_version_manifest(version_manifest_path)
    dataset_records: list[dict[str, Any]] = []
    dataset_missing: list[str] = []
    if not taxonomy_only:
        raw_graphs = [
            (AEA_SOURCE, AEA_GRAPH_URI),
            (WHO_ICTRP_SOURCE, WHO_ICTRP_GRAPH_URI),
        ]
        for source, graph_uri in raw_graphs:
            if not source.is_file():
                logger.info("Skipping optional graph; %s is absent", source)
                dataset_missing.append(str(source))
                continue
            if skip_existing_raw and graph_has_triples(graph_uri, graph_store_base):
                current_path = _input_record(source)["path"]
                prior_record = prior_dataset_records.get(current_path)
                if prior_record is not None:
                    logger.info(
                        "Skipping %s — already populated in TDB2 with a loader-owned snapshot version",
                        graph_uri,
                    )
                    dataset_records.append(prior_record)
                    continue
                # Safe first-upgrade migration for the Compose path: an
                # existing graph without a loader-owned record is not adopted
                # by assumption. Replace it from the exact local/R2 snapshot,
                # then hash that same input for datasetVersion.
                logger.warning(
                    "Reloading %s — existing TDB2 data has no verifiable loader manifest",
                    graph_uri,
                )
            put_named_graph(source, graph_uri, graph_store_base)
            dataset_records.append(_input_record(source))
    else:
        dataset_records.extend(prior_dataset_records.values())
        if not dataset_records:
            dataset_missing.append("loaded source snapshot manifest unavailable")
    taxonomy_payload = merge_turtle_sources(DEFAULT_TAXONOMY_SOURCES)
    put_named_graph_payload(taxonomy_payload, TAXONOMY_GRAPH_URI, graph_store_base)
    stamp_concept_matches(graph_store_base)
    if version_manifest_path is not None:
        write_version_manifest(
            version_manifest_path,
            build_version_manifest(
                taxonomy_sources=DEFAULT_TAXONOMY_SOURCES,
                dataset_sources=(AEA_SOURCE, WHO_ICTRP_SOURCE),
                dataset_records=dataset_records,
                dataset_missing=dataset_missing,
            ),
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Load Universal Evidence RDF files into Fuseki.")
    parser.add_argument(
        "--graph-store-url",
        default=DEFAULT_GRAPH_STORE_URL,
        help=f"Fuseki Graph Store endpoint. Default: {DEFAULT_GRAPH_STORE_URL}",
    )
    parser.add_argument(
        "--taxonomy-only",
        action="store_true",
        help="Skip raw study data graphs; only reload taxonomy + crosswalks. Fast path for crosswalk updates.",
    )
    parser.add_argument(
        "--skip-existing-raw",
        action="store_true",
        help=(
            "Skip a populated raw graph only when a loader manifest proves its "
            "snapshot; otherwise reload it once to establish datasetVersion."
        ),
    )
    parser.add_argument(
        "--version-manifest",
        type=Path,
        default=Path(
            os.getenv("UE_GRAPH_VERSION_MANIFEST", str(DEFAULT_VERSION_MANIFEST))
        ),
        help=(
            "Write the loader-owned taxonomy/dataset version manifest after a "
            "successful load."
        ),
    )
    return parser


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = build_parser().parse_args()

    try:
        sync_from_r2(taxonomy_only=args.taxonomy_only)
        load_default_graphs(
            args.graph_store_url,
            taxonomy_only=args.taxonomy_only,
            skip_existing_raw=args.skip_existing_raw,
            version_manifest_path=args.version_manifest,
        )
    except (FileNotFoundError, requests.RequestException, ConceptStampingError) as exc:
        logger.error("%s", exc)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
