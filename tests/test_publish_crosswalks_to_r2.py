from __future__ import annotations

from io import BytesIO
import json
from pathlib import Path
import sys

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import publish_crosswalks_to_r2 as publisher  # noqa: E402


class MissingObject(Exception):
    def __init__(self, key: str):
        super().__init__(key)
        self.response = {"Error": {"Code": "NoSuchKey"}}


class FakeS3:
    def __init__(self, objects: dict[str, bytes] | None = None):
        self.objects = dict(objects or {})
        self.writes: list[tuple[str, str]] = []
        self.put_payloads: list[tuple[str, bytes]] = []
        self.fail_copy_destination: str | None = None

    def head_object(self, *, Bucket: str, Key: str):
        del Bucket
        if Key not in self.objects:
            raise MissingObject(Key)
        return {"ContentLength": len(self.objects[Key])}

    def get_object(self, *, Bucket: str, Key: str):
        del Bucket
        if Key not in self.objects:
            raise MissingObject(Key)
        return {"Body": BytesIO(self.objects[Key])}

    def list_objects_v2(self, *, Bucket: str, Prefix: str, MaxKeys: int):
        del Bucket
        keys = sorted(key for key in self.objects if key.startswith(Prefix))[:MaxKeys]
        return {"KeyCount": len(keys), "Contents": [{"Key": key} for key in keys]}

    def copy_object(
        self,
        *,
        Bucket: str,
        Key: str,
        CopySource: dict[str, str],
        MetadataDirective: str | None = None,
    ):
        del Bucket, MetadataDirective
        if Key == self.fail_copy_destination:
            raise RuntimeError("injected promotion failure")
        source = CopySource["Key"]
        self.objects[Key] = self.objects[source]
        self.writes.append(("copy", Key))
        return {}

    def upload_file(
        self,
        Filename: str,
        Bucket: str,
        Key: str,
        ExtraArgs: dict[str, str] | None = None,
    ):
        del Bucket, ExtraArgs
        self.objects[Key] = Path(Filename).read_bytes()
        self.writes.append(("upload", Key))

    def put_object(
        self,
        *,
        Bucket: str,
        Key: str,
        Body: bytes,
        ContentType: str,
    ):
        del Bucket, ContentType
        self.objects[Key] = Body
        self.writes.append(("put", Key))
        self.put_payloads.append((Key, Body))
        return {}

    def delete_object(self, *, Bucket: str, Key: str):
        del Bucket
        self.objects.pop(Key, None)
        self.writes.append(("delete", Key))
        return {}


def _repo_fixture(tmp_path: Path, names: tuple[str, ...] = ("alpha", "beta")):
    crosswalk_dir = tmp_path / "vocabularies" / "crosswalks"
    crosswalk_dir.mkdir(parents=True)
    declarations = []
    for name in names:
        path = crosswalk_dir / f"{name}-crosswalk.ttl"
        path.write_text(f"# canonical {name}\n", encoding="utf-8")
        declarations.append(
            f'[] ue:crosswalk "vocabularies/crosswalks/{path.name}" .'
        )
    sources = tmp_path / "vocabularies" / "sources.ttl"
    sources.write_text(
        "@prefix ue: <https://universalevidence.com/ontology/> .\n"
        + "\n".join(declarations)
        + "\n",
        encoding="utf-8",
    )
    return sources, crosswalk_dir


def _objects(tmp_path: Path, names: tuple[str, ...] = ("alpha", "beta")):
    sources, _ = _repo_fixture(tmp_path, names)
    return publisher.derive_canonical_objects(
        sources, tmp_path, expected_count=len(names)
    )


def test_production_registry_declares_exact_canonical_allowlist():
    objects = publisher.derive_canonical_objects()

    assert len(objects) == publisher.EXPECTED_CANONICAL_CROSSWALK_COUNT == 18
    assert len({item.active_key for item in objects}) == 18
    assert all(item.active_key.startswith("crosswalks/") for item in objects)
    assert all(" 2.ttl" not in item.active_key for item in objects)


def test_registry_allowlist_ignores_unreferenced_conflict_copy(tmp_path):
    sources, crosswalk_dir = _repo_fixture(tmp_path)
    (crosswalk_dir / "alpha-crosswalk 2.ttl").write_text(
        "# iCloud conflict copy\n", encoding="utf-8"
    )

    objects = publisher.derive_canonical_objects(
        sources, tmp_path, expected_count=2
    )

    assert [item.active_key for item in objects] == [
        "crosswalks/alpha-crosswalk.ttl",
        "crosswalks/beta-crosswalk.ttl",
    ]


def test_registry_rejects_declared_conflict_copy(tmp_path):
    _, crosswalk_dir = _repo_fixture(tmp_path, ("alpha",))
    conflict = crosswalk_dir / "alpha-crosswalk 2.ttl"
    conflict.write_text("# conflict\n", encoding="utf-8")
    sources = tmp_path / "vocabularies" / "sources.ttl"
    sources.write_text(
        "@prefix ue: <https://universalevidence.com/ontology/> .\n"
        '[] ue:crosswalk "vocabularies/crosswalks/alpha-crosswalk 2.ttl" .\n',
        encoding="utf-8",
    )

    with pytest.raises(publisher.PublishError, match="unsafe or noncanonical"):
        publisher.derive_canonical_objects(sources, tmp_path, expected_count=1)


def test_registry_count_is_a_hard_gate(tmp_path):
    sources, _ = _repo_fixture(tmp_path, ("alpha",))

    with pytest.raises(publisher.PublishError, match="expected exactly 18"):
        publisher.derive_canonical_objects(sources, tmp_path)


def test_dry_run_streams_active_objects_without_writes(tmp_path):
    objects = _objects(tmp_path)
    alpha = objects[0]
    client = FakeS3({alpha.active_key: b"old alpha\n"})

    manifest = publisher.dry_run(
        client, "bucket", objects, backup_prefix="backups/release-dry-run"
    )

    assert manifest["status"] == "dry-run-complete"
    assert [record["action"] for record in manifest["objects"]] == [
        "replace",
        "create",
    ]
    assert client.writes == []


def test_execute_backs_up_stages_and_verifies_only_allowlisted_objects(tmp_path):
    objects = _objects(tmp_path)
    alpha, beta = objects
    conflict_key = "crosswalks/alpha-crosswalk 2.ttl"
    old_alpha = b"old alpha\n"
    client = FakeS3(
        {
            alpha.active_key: old_alpha,
            conflict_key: b"must remain untouched\n",
        }
    )
    manifest_path = tmp_path / "reports" / "release.json"

    manifest = publisher.execute_release(
        client,
        "bucket",
        objects,
        backup_prefix="backups/release-2026-08-17T081500Z",
        manifest_path=manifest_path,
    )

    assert manifest["status"] == "complete"
    assert client.objects[alpha.active_key] == alpha.local_path.read_bytes()
    assert client.objects[beta.active_key] == beta.local_path.read_bytes()
    assert client.objects[conflict_key] == b"must remain untouched\n"
    assert all(conflict_key not in key for _, key in client.writes)

    backup_key = (
        "backups/release-2026-08-17T081500Z/active/" + alpha.active_key
    )
    assert client.objects[backup_key] == old_alpha
    assert (
        "backups/release-2026-08-17T081500Z/manifest.json" in client.objects
    )

    manifest_key = "backups/release-2026-08-17T081500Z/manifest.json"
    first_manifest_write = client.writes.index(("put", manifest_key))
    first_active_promotion = client.writes.index(("copy", alpha.active_key))
    assert first_manifest_write < first_active_promotion
    assert json.loads(client.put_payloads[0][1])["status"] == "prepared"
    assert json.loads(client.put_payloads[-1][1])["status"] == "complete"

    disk_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    records = {record["active_key"]: record for record in disk_manifest["objects"]}
    assert records[alpha.active_key]["backup"]["sha256"] == publisher.hashlib.sha256(
        old_alpha
    ).hexdigest()
    assert records[alpha.active_key]["active_after"] == {
        "exists": True,
        **records[alpha.active_key]["local"],
        "verified_at": records[alpha.active_key]["active_after"]["verified_at"],
    }
    assert records[beta.active_key]["backup"] == {
        "exists": False,
        "reason": "active-missing",
    }


def test_execute_refuses_reused_backup_prefix_before_writing(tmp_path):
    objects = _objects(tmp_path, ("alpha",))
    client = FakeS3({"backups/already-used/manifest.json": b"{}\n"})

    with pytest.raises(publisher.PublishError, match="not unique/empty"):
        publisher.execute_release(
            client,
            "bucket",
            objects,
            backup_prefix="backups/already-used",
            manifest_path=tmp_path / "release.json",
        )

    assert client.writes == []


def test_failed_promotion_restores_preexisting_active_object(tmp_path):
    objects = _objects(tmp_path)
    alpha, beta = objects
    old_alpha = b"old alpha\n"
    client = FakeS3({alpha.active_key: old_alpha})
    client.fail_copy_destination = beta.active_key
    manifest_path = tmp_path / "failed-release.json"

    with pytest.raises(publisher.PublishError, match="rolled back"):
        publisher.execute_release(
            client,
            "bucket",
            objects,
            backup_prefix="backups/release-with-failure",
            manifest_path=manifest_path,
        )

    assert client.objects[alpha.active_key] == old_alpha
    assert beta.active_key not in client.objects
    failed = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert failed["status"] == "rolled-back"
    alpha_record = next(
        record for record in failed["objects"] if record["active_key"] == alpha.active_key
    )
    assert alpha_record["rollback"]["status"] == "restored"


def test_execute_cli_requires_explicit_backup_and_manifest():
    with pytest.raises(SystemExit):
        publisher.parse_args(["--execute"])
    with pytest.raises(SystemExit):
        publisher.parse_args(["--execute", "--backup-prefix", "backups/new"])
