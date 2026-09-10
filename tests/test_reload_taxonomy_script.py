from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shlex
import subprocess
import tarfile

from scripts import load_fuseki


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "deploy" / "reload_taxonomy.sh"


def _run_bash(source: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", source],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def _source_script() -> str:
    return f"source {shlex.quote(str(SCRIPT))}"


def test_reload_script_has_valid_bash_syntax():
    result = subprocess.run(
        ["bash", "-n", str(SCRIPT)],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_version_manifest_verifier_accepts_exact_release_and_rejects_mismatch(tmp_path):
    manifest_dir = tmp_path / "loader-manifests"
    manifest_path = manifest_dir / "graph-versions.json"
    manifest = load_fuseki.build_version_manifest()
    assert manifest["taxonomyVersion"]
    assert manifest["datasetVersion"]
    load_fuseki.write_version_manifest(manifest_path, manifest)

    setup = f"""
{_source_script()}
PATH={shlex.quote(str(REPO_ROOT / '.venv' / 'bin'))}:$PATH
TAXONOMY_RELOAD_MANIFEST_PATH={shlex.quote(str(manifest_path))}
EXPECTED_TAXONOMY_VERSION={shlex.quote(manifest['taxonomyVersion'])}
EXPECTED_DATASET_VERSION={shlex.quote(manifest['datasetVersion'])}
docker() {{
  local -a environment=()
  shift  # compose
  shift  # run
  while (($#)); do
    case $1 in
      --rm|--no-deps) shift ;;
      -e) environment+=("$2"); shift 2 ;;
      loader) shift; env "${{environment[@]}}" "$@"; return ;;
      *) return 97 ;;
    esac
  done
}}
"""
    accepted = _run_bash(setup + "\nverify_version_manifest\n")
    assert accepted.returncode == 0, accepted.stderr

    rejected = _run_bash(
        setup
        + "\nEXPECTED_TAXONOMY_VERSION="
        + ("0" * 64)
        + "\nverify_version_manifest\n"
    )
    assert rejected.returncode != 0
    assert "taxonomyVersion mismatch" in rejected.stderr


def test_stamp_verifier_requires_all_six_positive_stamp_sets():
    graph_aea = "https://universalevidence.com/graph/aea"
    graph_who = "https://universalevidence.com/graph/who-ictrp"
    ontology = "https://universalevidence.com/ontology/"
    bindings = []
    for graph in (graph_aea, graph_who):
        for predicate in ("matchesCondition", "matchesIntervention", "matchesOutcome"):
            bindings.append(
                {
                    "graph": {"type": "uri", "value": graph},
                    "predicate": {"type": "uri", "value": ontology + predicate},
                    "count": {"type": "literal", "value": "1"},
                }
            )
    payload = json.dumps({"results": {"bindings": bindings}})
    command = f"""
{_source_script()}
curl() {{ printf '%s' {shlex.quote(payload)}; }}
verify_required_stamps
"""

    accepted = _run_bash(command)
    assert accepted.returncode == 0, accepted.stderr
    assert "Verified required concept stamps" in accepted.stdout

    missing_payload = json.dumps({"results": {"bindings": bindings[:-1]}})
    rejected = _run_bash(
        f"""
{_source_script()}
curl() {{ printf '%s' {shlex.quote(missing_payload)}; }}
verify_required_stamps
"""
    )
    assert rejected.returncode != 0
    assert "required stamp verification failed" in rejected.stderr


def test_paired_backup_verifier_rejects_a_tampered_archive(tmp_path):
    bundle = tmp_path / "taxonomy-reload-test"
    bundle.mkdir()
    records = {}
    for key, volume_name in (
        ("fuseki-data", "ue_fuseki-data"),
        ("loader-manifests", "ue_loader-manifests"),
    ):
        payload = tmp_path / f"{key}.txt"
        payload.write_text(key, encoding="utf-8")
        archive = bundle / f"{key}.tar.gz"
        with tarfile.open(archive, "w:gz") as handle:
            handle.add(payload, arcname=payload.name)
        records[key] = {
            "name": volume_name,
            "archive": archive.name,
            "size": archive.stat().st_size,
            "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        }
    (bundle / "backup-manifest.json").write_text(
        json.dumps(
            {"schemaVersion": "ue-taxonomy-backup-v1", "volumes": records}
        ),
        encoding="utf-8",
    )
    setup = f"""
{_source_script()}
backup_bundle={shlex.quote(str(bundle))}
fuseki_volume_name=ue_fuseki-data
manifest_volume_name=ue_loader-manifests
"""

    accepted = _run_bash(setup + "\nverify_backup_bundle\n")
    assert accepted.returncode == 0, accepted.stderr

    with (bundle / "loader-manifests.tar.gz").open("ab") as handle:
        handle.write(b"tampered")
    rejected = _run_bash(setup + "\nverify_backup_bundle\n")
    assert rejected.returncode != 0
    assert "wrong size" in rejected.stderr or "checksum mismatch" in rejected.stderr
