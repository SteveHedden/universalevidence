#!/usr/bin/env python3
"""Safely publish the canonical crosswalk set to Cloudflare R2.

Unlike ``upload_to_r2.py``, this command never scans a directory for upload
targets.  Its allowlist is the exact set of ``ue:crosswalk`` paths declared in
``vocabularies/sources.ttl``.  A production write requires ``--execute``, a
previously unused backup prefix, and a new local manifest path.

Execution is intentionally multi-phase:

1. hash every local and currently active object;
2. server-side copy every active canonical object to the backup prefix and
   stream-download/hash the copy;
3. upload changed canonical files to a staging area under that prefix and
   stream-download/hash them;
4. confirm each active object is still the version seen during preflight,
   promote the staged object, and stream-download/hash the new active object;
5. write and verify a release manifest under the backup prefix.

No credential values or endpoint URLs are logged.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path, PurePosixPath
import re
import sys
import tempfile
from typing import Any, Iterable

from rdflib import Graph, Namespace


logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCES_FILE = REPO_ROOT / "vocabularies" / "sources.ttl"
EXPECTED_CANONICAL_CROSSWALK_COUNT = 18
UE = Namespace("https://universalevidence.com/ontology/")
CHUNK_SIZE = 8 * 1024 * 1024
MANIFEST_SCHEMA = "ue-r2-canonical-crosswalk-release-v1"

# iCloud conflict copies seen in this repository include names such as
# ``ctgov-regions-crosswalk 2.ttl``.  They must never become release inputs,
# even if a malformed source registry accidentally references one.
_UNSAFE_FILENAME = re.compile(r"(?:conflict|conflicted|copy|\s+\d+\.ttl$)", re.I)


class PublishError(RuntimeError):
    """Raised when a release safety invariant fails."""


@dataclass(frozen=True)
class CanonicalObject:
    """One source-registry-declared crosswalk and its exact R2 key."""

    registry_path: str
    local_path: Path
    active_key: str


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_dotenv(repo_root: Path = REPO_ROOT) -> None:
    """Load unset R2 variables without displaying their values."""

    env_file = repo_root / ".env"
    if not env_file.is_file():
        return
    for raw_line in env_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def build_client() -> tuple[Any, str]:
    endpoint = os.environ.get("R2_ENDPOINT_URL")
    access_key = os.environ.get("R2_ACCESS_KEY_ID")
    secret_key = os.environ.get("R2_SECRET_ACCESS_KEY")
    bucket = os.environ.get("R2_BUCKET", "universalevidence-data")
    if not all((endpoint, access_key, secret_key)):
        raise PublishError(
            "R2 credentials are missing; set R2_ENDPOINT_URL, "
            "R2_ACCESS_KEY_ID, and R2_SECRET_ACCESS_KEY"
        )

    import boto3
    from botocore.config import Config

    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name="auto",
        config=Config(read_timeout=300, retries={"mode": "standard", "max_attempts": 10}),
    )
    return client, bucket


def derive_canonical_objects(
    sources_file: Path = DEFAULT_SOURCES_FILE,
    repo_root: Path = REPO_ROOT,
    *,
    expected_count: int = EXPECTED_CANONICAL_CROSSWALK_COUNT,
) -> list[CanonicalObject]:
    """Return the exact crosswalk allowlist declared by ``sources.ttl``.

    Paths must be direct children of ``vocabularies/crosswalks``.  Directory
    contents that are not declared in the registry are never considered.
    """

    sources_file = sources_file.resolve()
    repo_root = repo_root.resolve()
    graph = Graph().parse(sources_file, format="turtle")
    declared = sorted({str(value) for value in graph.objects(None, UE.crosswalk)})
    if len(declared) != expected_count:
        raise PublishError(
            f"sources registry declares {len(declared)} unique crosswalks; "
            f"expected exactly {expected_count}"
        )

    crosswalk_dir = (repo_root / "vocabularies" / "crosswalks").resolve()
    objects: list[CanonicalObject] = []
    active_keys: set[str] = set()
    for registry_path in declared:
        pure = PurePosixPath(registry_path)
        if (
            pure.is_absolute()
            or pure.parts[:2] != ("vocabularies", "crosswalks")
            or len(pure.parts) != 3
            or pure.suffix != ".ttl"
            or _UNSAFE_FILENAME.search(pure.name)
        ):
            raise PublishError(
                f"unsafe or noncanonical ue:crosswalk path in source registry: {registry_path}"
            )

        local_path = (repo_root / Path(*pure.parts)).resolve()
        if local_path.parent != crosswalk_dir:
            raise PublishError(f"crosswalk escapes canonical directory: {registry_path}")
        if not local_path.is_file() or local_path.is_symlink():
            raise PublishError(f"canonical crosswalk is missing or not a regular file: {registry_path}")

        active_key = f"crosswalks/{pure.name}"
        if active_key in active_keys:
            raise PublishError(f"duplicate canonical R2 key: {active_key}")
        active_keys.add(active_key)
        objects.append(CanonicalObject(registry_path, local_path, active_key))

    return objects


def sha256_file(path: Path) -> dict[str, Any]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK_SIZE):
            digest.update(chunk)
            size += len(chunk)
    return {"size": size, "sha256": digest.hexdigest()}


def _is_missing_error(error: BaseException) -> bool:
    response = getattr(error, "response", None)
    if isinstance(response, dict):
        code = str(response.get("Error", {}).get("Code", ""))
        return code in {"404", "NoSuchKey", "NotFound"}
    return isinstance(error, (FileNotFoundError, KeyError))


def head_object_or_none(client: Any, bucket: str, key: str) -> dict[str, Any] | None:
    try:
        return client.head_object(Bucket=bucket, Key=key)
    except Exception as error:  # boto clients expose provider-specific subclasses
        if _is_missing_error(error):
            return None
        raise


def stream_object_digest(client: Any, bucket: str, key: str) -> dict[str, Any]:
    """Download an object as a stream and return its observed size and hash."""

    response = client.get_object(Bucket=bucket, Key=key)
    body = response["Body"]
    digest = hashlib.sha256()
    size = 0
    try:
        while chunk := body.read(CHUNK_SIZE):
            digest.update(chunk)
            size += len(chunk)
    finally:
        close = getattr(body, "close", None)
        if close:
            close()
    return {"size": size, "sha256": digest.hexdigest()}


def inspect_active_object(client: Any, bucket: str, key: str) -> dict[str, Any]:
    head = head_object_or_none(client, bucket, key)
    if head is None:
        return {"exists": False}
    observed = stream_object_digest(client, bucket, key)
    declared_size = int(head.get("ContentLength", observed["size"]))
    if declared_size != observed["size"]:
        raise PublishError(
            f"R2 object size changed while hashing {key}: "
            f"HEAD={declared_size}, streamed={observed['size']}"
        )
    return {"exists": True, **observed}


def validate_backup_prefix(prefix: str) -> str:
    normalized = prefix.strip().strip("/")
    parts = PurePosixPath(normalized).parts
    if (
        not normalized
        or normalized != prefix.strip().strip("/")
        or "\\" in normalized
        or any(part in {"", ".", ".."} for part in parts)
        or any(ord(character) < 32 for character in normalized)
        or normalized == "crosswalks"
        or normalized.startswith("crosswalks/")
    ):
        raise PublishError(f"unsafe backup prefix: {prefix!r}")
    return normalized


def assert_prefix_unused(client: Any, bucket: str, prefix: str) -> None:
    response = client.list_objects_v2(Bucket=bucket, Prefix=f"{prefix}/", MaxKeys=1)
    if response.get("Contents") or int(response.get("KeyCount", 0)):
        raise PublishError(
            f"backup prefix is not unique/empty: s3://{bucket}/{prefix}/"
        )


def _same_digest(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return left.get("size") == right.get("size") and left.get("sha256") == right.get(
        "sha256"
    )


def _verify_digest(
    *, key: str, expected: dict[str, Any], observed: dict[str, Any], phase: str
) -> None:
    if not _same_digest(expected, observed):
        raise PublishError(
            f"{phase} verification failed for {key}: expected "
            f"{expected.get('size')} bytes/{expected.get('sha256')}, observed "
            f"{observed.get('size')} bytes/{observed.get('sha256')}"
        )


def _write_manifest(path: Path, manifest: dict[str, Any]) -> bytes:
    payload = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("wb", dir=path.parent, delete=False) as handle:
        handle.write(payload)
        temporary = Path(handle.name)
    temporary.replace(path)
    return payload


def _manifest_base(
    *, mode: str, bucket: str, backup_prefix: str | None, records: list[dict[str, Any]]
) -> dict[str, Any]:
    return {
        "schema_version": MANIFEST_SCHEMA,
        "mode": mode,
        "status": "preflight",
        "created_at": utc_now(),
        "completed_at": None,
        "bucket": bucket,
        "backup_prefix": backup_prefix,
        "canonical_count": len(records),
        "objects": records,
    }


def build_local_records(objects: Iterable[CanonicalObject]) -> list[dict[str, Any]]:
    return [
        {
            "registry_path": item.registry_path,
            "active_key": item.active_key,
            "local": sha256_file(item.local_path),
        }
        for item in objects
    ]


def dry_run(
    client: Any,
    bucket: str,
    objects: list[CanonicalObject],
    *,
    backup_prefix: str | None = None,
) -> dict[str, Any]:
    """Build a read-only plan, including streamed hashes of active objects."""

    normalized_prefix = validate_backup_prefix(backup_prefix) if backup_prefix else None
    if normalized_prefix:
        assert_prefix_unused(client, bucket, normalized_prefix)
    records = build_local_records(objects)
    for record in records:
        active = inspect_active_object(client, bucket, record["active_key"])
        record["active_before"] = active
        record["action"] = (
            "create"
            if not active["exists"]
            else "unchanged"
            if _same_digest(record["local"], active)
            else "replace"
        )
    manifest = _manifest_base(
        mode="dry-run", bucket=bucket, backup_prefix=normalized_prefix, records=records
    )
    manifest["status"] = "dry-run-complete"
    manifest["completed_at"] = utc_now()
    return manifest


def _assert_active_unchanged(
    client: Any, bucket: str, key: str, before: dict[str, Any]
) -> None:
    current = inspect_active_object(client, bucket, key)
    if current.get("exists") != before.get("exists"):
        raise PublishError(f"active object changed after preflight: {key}")
    if current.get("exists") and not _same_digest(current, before):
        raise PublishError(f"active object changed after preflight: {key}")


def _publish_manifest(
    client: Any,
    bucket: str,
    key: str,
    payload: bytes,
) -> dict[str, Any]:
    expected = {"size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}
    client.put_object(
        Bucket=bucket,
        Key=key,
        Body=payload,
        ContentType="application/json",
    )
    observed = stream_object_digest(client, bucket, key)
    _verify_digest(key=key, expected=expected, observed=observed, phase="manifest")
    return observed


def _rollback_promotions(
    client: Any,
    bucket: str,
    promoted: list[dict[str, Any]],
) -> list[str]:
    """Best-effort rollback that refuses to clobber a concurrent later write."""

    errors: list[str] = []
    for record in reversed(promoted):
        key = record["active_key"]
        try:
            current = inspect_active_object(client, bucket, key)
            if not current.get("exists") or not _same_digest(current, record["local"]):
                raise PublishError(
                    "active object no longer matches this release; refusing rollback"
                )
            before = record["active_before"]
            if before["exists"]:
                client.copy_object(
                    Bucket=bucket,
                    Key=key,
                    CopySource={"Bucket": bucket, "Key": record["backup_key"]},
                )
                restored = stream_object_digest(client, bucket, key)
                _verify_digest(
                    key=key, expected=before, observed=restored, phase="rollback"
                )
            else:
                client.delete_object(Bucket=bucket, Key=key)
                if head_object_or_none(client, bucket, key) is not None:
                    raise PublishError("new active object still exists after rollback delete")
            record["rollback"] = {"status": "restored", "verified_at": utc_now()}
        except Exception as error:  # preserve all rollback failures in the manifest
            message = f"{key}: {error}"
            record["rollback"] = {"status": "failed", "error": message}
            errors.append(message)
    return errors


def execute_release(
    client: Any,
    bucket: str,
    objects: list[CanonicalObject],
    *,
    backup_prefix: str,
    manifest_path: Path,
) -> dict[str, Any]:
    """Back up, stage, promote, and verify the canonical crosswalk release."""

    prefix = validate_backup_prefix(backup_prefix)
    manifest_path = manifest_path.resolve()
    if manifest_path.exists():
        raise PublishError(f"refusing to overwrite existing manifest: {manifest_path}")
    assert_prefix_unused(client, bucket, prefix)

    records = build_local_records(objects)
    object_by_key = {item.active_key: item for item in objects}
    manifest = _manifest_base(
        mode="execute", bucket=bucket, backup_prefix=prefix, records=records
    )
    manifest_key = f"{prefix}/manifest.json"
    manifest["manifest_key"] = manifest_key
    promoted: list[dict[str, Any]] = []

    try:
        # Preflight every active object before any R2 write.
        for record in records:
            active = inspect_active_object(client, bucket, record["active_key"])
            record["active_before"] = active
            record["action"] = (
                "create"
                if not active["exists"]
                else "unchanged"
                if _same_digest(record["local"], active)
                else "replace"
            )

        # Back up and stream-verify every active canonical object first.  This
        # creates a complete pre-release snapshot, including unchanged files.
        manifest["status"] = "backing-up"
        for record in records:
            if not record["active_before"]["exists"]:
                record["backup"] = {"exists": False, "reason": "active-missing"}
                continue
            backup_key = f"{prefix}/active/{record['active_key']}"
            record["backup_key"] = backup_key
            client.copy_object(
                Bucket=bucket,
                Key=backup_key,
                CopySource={"Bucket": bucket, "Key": record["active_key"]},
            )
            backup = stream_object_digest(client, bucket, backup_key)
            _verify_digest(
                key=backup_key,
                expected=record["active_before"],
                observed=backup,
                phase="backup",
            )
            record["backup"] = {"exists": True, **backup, "verified_at": utc_now()}

        # Stage and stream-verify changed/new canonical objects.  Unchanged
        # active objects need no upload or promotion.
        manifest["status"] = "staging"
        for record in records:
            if record["action"] == "unchanged":
                record["staged"] = {"exists": False, "reason": "unchanged"}
                continue
            stage_key = f"{prefix}/staged/{record['active_key']}"
            record["stage_key"] = stage_key
            item = object_by_key[record["active_key"]]
            client.upload_file(
                str(item.local_path),
                bucket,
                stage_key,
                ExtraArgs={"ContentType": "text/turtle"},
            )
            staged = stream_object_digest(client, bucket, stage_key)
            _verify_digest(
                key=stage_key,
                expected=record["local"],
                observed=staged,
                phase="staging",
            )
            record["staged"] = {"exists": True, **staged, "verified_at": utc_now()}

        # Persist a recovery ledger before the first active-key mutation.  If
        # the process or host dies during promotion, this prepared manifest
        # names every verified backup and staged object needed for recovery.
        manifest["status"] = "prepared"
        manifest["prepared_at"] = utc_now()
        prepared_payload = _write_manifest(manifest_path, manifest)
        _publish_manifest(client, bucket, manifest_key, prepared_payload)

        # Promote only after every backup and staged upload has been verified.
        manifest["status"] = "promoting"
        for record in records:
            if record["action"] == "unchanged":
                continue
            _assert_active_unchanged(
                client, bucket, record["active_key"], record["active_before"]
            )
            client.copy_object(
                Bucket=bucket,
                Key=record["active_key"],
                CopySource={"Bucket": bucket, "Key": record["stage_key"]},
                MetadataDirective="COPY",
            )
            promoted.append(record)
            active_after = stream_object_digest(client, bucket, record["active_key"])
            _verify_digest(
                key=record["active_key"],
                expected=record["local"],
                observed=active_after,
                phase="active",
            )
            record["active_after"] = {
                "exists": True,
                **active_after,
                "verified_at": utc_now(),
            }

        # Verify unchanged objects too, so the manifest describes the complete
        # canonical active set at release completion.
        for record in records:
            if record["action"] != "unchanged":
                continue
            active_after = inspect_active_object(client, bucket, record["active_key"])
            _verify_digest(
                key=record["active_key"],
                expected=record["local"],
                observed=active_after,
                phase="active",
            )
            record["active_after"] = {
                **active_after,
                "verified_at": utc_now(),
            }

        manifest["status"] = "complete"
        manifest["completed_at"] = utc_now()
        payload = _write_manifest(manifest_path, manifest)
        manifest_observed = _publish_manifest(client, bucket, manifest_key, payload)
        logger.info(
            "Published %d canonical crosswalks; manifest %s (%d bytes, sha256=%s)",
            len(records),
            manifest_key,
            manifest_observed["size"],
            manifest_observed["sha256"],
        )
        return manifest
    except Exception as error:
        rollback_errors = _rollback_promotions(client, bucket, promoted)
        manifest["status"] = (
            "rollback-failed"
            if rollback_errors
            else "rolled-back"
            if promoted
            else "failed-before-promotion"
        )
        manifest["completed_at"] = utc_now()
        manifest["error"] = str(error)
        manifest["rollback_errors"] = rollback_errors
        payload = _write_manifest(manifest_path, manifest)
        try:
            _publish_manifest(client, bucket, manifest_key, payload)
        except Exception as manifest_error:
            logger.error("Could not publish failure manifest: %s", manifest_error)
        if rollback_errors:
            raise PublishError(
                f"release failed ({error}); rollback also failed: "
                + "; ".join(rollback_errors)
            ) from error
        raise PublishError(f"release failed and was rolled back: {error}") from error


def _print_local_list(objects: list[CanonicalObject]) -> None:
    for item in objects:
        digest = sha256_file(item.local_path)
        print(
            f"{item.registry_path}\t{item.active_key}\t"
            f"{digest['size']}\t{digest['sha256']}"
        )
    print(f"canonical_count\t{len(objects)}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Safely publish only source-registry-declared R2 crosswalks."
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--list", action="store_true", help="List/hash canonical local files; no R2 access."
    )
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Read/hash active R2 objects and show the plan; perform no R2 writes.",
    )
    mode.add_argument(
        "--execute",
        action="store_true",
        help="Perform the backed-up, staged, verified canonical release.",
    )
    parser.add_argument(
        "--backup-prefix",
        help="Caller-supplied unused prefix (required with --execute).",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        help="New local JSON manifest path (required with --execute; optional for --dry-run).",
    )
    parser.add_argument("--sources-file", type=Path, default=DEFAULT_SOURCES_FILE)
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    args = parser.parse_args(argv)
    if args.execute and not args.backup_prefix:
        parser.error("--execute requires --backup-prefix")
    if args.execute and not args.manifest:
        parser.error("--execute requires --manifest")
    return args


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = parse_args(argv)
    try:
        objects = derive_canonical_objects(
            sources_file=args.sources_file,
            repo_root=args.repo_root,
        )
        if args.list:
            _print_local_list(objects)
            return 0

        load_dotenv(args.repo_root)
        client, bucket = build_client()
        if args.dry_run:
            manifest = dry_run(
                client,
                bucket,
                objects,
                backup_prefix=args.backup_prefix,
            )
            payload = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
            if args.manifest:
                if args.manifest.exists():
                    raise PublishError(
                        f"refusing to overwrite existing manifest: {args.manifest.resolve()}"
                    )
                _write_manifest(args.manifest, manifest)
                logger.info("Wrote dry-run manifest: %s", args.manifest.resolve())
            else:
                print(payload, end="")
            return 0

        execute_release(
            client,
            bucket,
            objects,
            backup_prefix=args.backup_prefix,
            manifest_path=args.manifest,
        )
        return 0
    except (OSError, PublishError) as error:
        logger.error("%s", error)
        return 1


if __name__ == "__main__":
    sys.exit(main())
