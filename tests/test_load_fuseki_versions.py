from __future__ import annotations

import json

import pytest
from rdflib import Dataset, Literal, Namespace, RDF, URIRef

from scripts import load_fuseki


def test_loader_versions_are_stable_and_content_addressed(tmp_path):
    taxonomy = tmp_path / "states.ttl"
    crosswalk = tmp_path / "crosswalk.ttl"
    dataset = tmp_path / "aea.ttl"
    taxonomy.write_text("@prefix ex: <https://example.org/> . ex:a ex:p ex:b .\n")
    crosswalk.write_text("@prefix ex: <https://example.org/> . ex:x ex:p ex:y .\n")
    dataset.write_text("@prefix ex: <https://example.org/> . ex:s ex:p ex:o .\n")

    first = load_fuseki.build_version_manifest(
        taxonomy_sources=(taxonomy, crosswalk),
        dataset_sources=(dataset,),
    )
    second = load_fuseki.build_version_manifest(
        taxonomy_sources=(taxonomy, crosswalk),
        dataset_sources=(dataset,),
    )

    assert first == second
    assert len(first["taxonomyVersion"]) == 64
    assert len(first["datasetVersion"]) == 64

    crosswalk.write_text("@prefix ex: <https://example.org/> . ex:x ex:p ex:z .\n")
    changed = load_fuseki.build_version_manifest(
        taxonomy_sources=(taxonomy, crosswalk),
        dataset_sources=(dataset,),
    )

    assert changed["taxonomyVersion"] != first["taxonomyVersion"]
    assert changed["datasetVersion"] == first["datasetVersion"]


def test_missing_loader_input_withholds_affected_version(tmp_path):
    taxonomy = tmp_path / "states.ttl"
    taxonomy.write_text("@prefix ex: <https://example.org/> . ex:a ex:p ex:b .\n")

    manifest = load_fuseki.build_version_manifest(
        taxonomy_sources=(taxonomy, tmp_path / "missing-crosswalk.ttl"),
        dataset_sources=(tmp_path / "missing-dataset.ttl",),
    )

    assert manifest["taxonomyVersion"] is None
    assert manifest["datasetVersion"] is None
    assert manifest["taxonomy"]["missing"]
    assert manifest["dataset"]["missing"]


def test_loader_manifest_round_trips_atomically(tmp_path):
    target = tmp_path / "manifests" / "graph-versions.json"
    payload = {
        "schemaVersion": "ue-loader-versions-v1",
        "taxonomyVersion": "a" * 64,
        "datasetVersion": "b" * 64,
        "taxonomy": {"inputs": [], "missing": []},
        "dataset": {"inputs": [], "missing": []},
    }

    load_fuseki.write_version_manifest(target, payload)

    assert load_fuseki.read_version_manifest(target) == payload
    assert json.loads(target.read_text()) == payload


def test_graph_store_puts_allow_large_payloads_to_finish(monkeypatch, tmp_path):
    source = tmp_path / "source.ttl"
    source.write_text("@prefix ex: <https://example.org/> . ex:s ex:p ex:o .\n")
    timeouts = []

    class Response:
        @staticmethod
        def raise_for_status():
            return None

    def record_put(*_args, **kwargs):
        timeouts.append(kwargs["timeout"])
        return Response()

    monkeypatch.setattr(load_fuseki.requests, "put", record_put)

    load_fuseki.put_named_graph(
        source,
        "https://example.org/graph/source",
        "http://fuseki:3030/ue/data",
    )
    load_fuseki.put_named_graph_payload(
        source.read_bytes(),
        "https://example.org/graph/payload",
        "http://fuseki:3030/ue/data",
    )

    assert timeouts == [
        load_fuseki.GRAPH_STORE_UPLOAD_TIMEOUT_SECONDS,
        load_fuseki.GRAPH_STORE_UPLOAD_TIMEOUT_SECONDS,
    ]
    assert load_fuseki.GRAPH_STORE_UPLOAD_TIMEOUT_SECONDS >= 600


def test_concept_stamp_failure_unpublishes_prior_manifest(monkeypatch, tmp_path):
    taxonomy = tmp_path / "states.ttl"
    dataset = tmp_path / "aea.ttl"
    taxonomy.write_text("@prefix ex: <https://example.org/> . ex:a ex:p ex:b .\n")
    dataset.write_text("@prefix ex: <https://example.org/> . ex:s ex:p ex:o .\n")
    manifest_path = tmp_path / "graph-versions.json"
    load_fuseki.write_version_manifest(
        manifest_path,
        load_fuseki.build_version_manifest(
            taxonomy_sources=(taxonomy,), dataset_sources=(dataset,)
        ),
    )

    monkeypatch.setattr(load_fuseki, "DEFAULT_TAXONOMY_SOURCES", (taxonomy,))
    monkeypatch.setattr(load_fuseki, "merge_turtle_sources", lambda _paths: b"ttl")
    monkeypatch.setattr(load_fuseki, "put_named_graph_payload", lambda *_args: None)

    def fail_stamping(_graph_store_base):
        raise load_fuseki.ConceptStampingError("required stamp failed")

    monkeypatch.setattr(load_fuseki, "stamp_concept_matches", fail_stamping)

    with pytest.raises(load_fuseki.ConceptStampingError):
        load_fuseki.load_default_graphs(
            taxonomy_only=True,
            version_manifest_path=manifest_path,
        )

    assert not manifest_path.exists()


def test_concept_stamp_pass_raises_when_any_required_stamp_fails(monkeypatch):
    def fail_update(*_args):
        raise RuntimeError("update failed")

    monkeypatch.setattr(load_fuseki, "_clear_stamp_predicate", fail_update)

    with pytest.raises(load_fuseki.ConceptStampingError) as failure:
        load_fuseki.stamp_concept_matches("http://fuseki:3030/ue/data")

    assert "AEA matchesCondition" in str(failure.value)
    assert "WHO ICTRP matchesOutcome" in str(failure.value)


def test_aea_intervention_stamping_joins_exact_and_normalized_literals(monkeypatch):
    queries: list[str] = []
    inserted: list[tuple[str, str]] = []
    dataset = Dataset()
    ue = Namespace("https://universalevidence.com/ontology/")
    aea = Namespace("https://socialscienceregistry.org/schema#")
    skos = Namespace("http://www.w3.org/2004/02/skos/core#")
    aea_graph = dataset.graph(URIRef(load_fuseki.AEA_GRAPH_URI))
    taxonomy_graph = dataset.graph(URIRef(load_fuseki.TAXONOMY_GRAPH_URI))
    exact_study = URIRef("https://example.org/study/exact")
    normalized_study = URIRef("https://example.org/study/normalized")
    exact_concept = URIRef("https://example.org/intervention/exact")
    normalized_concept = URIRef("https://example.org/intervention/normalized")

    aea_graph.add((exact_study, aea.intervention, Literal("Line one\r\nLine two")))
    aea_graph.add(
        (normalized_study, aea.intervention, Literal("  Older\tproducer\ntext  "))
    )
    taxonomy_graph.add(
        (
            URIRef("https://example.org/entry/exact"),
            ue.rawText,
            Literal("Line one\r\nLine two"),
        )
    )
    taxonomy_graph.add(
        (URIRef("https://example.org/entry/exact"), skos.closeMatch, exact_concept)
    )
    taxonomy_graph.add((exact_concept, RDF.type, ue.Intervention))
    taxonomy_graph.add(
        (
            URIRef("https://example.org/entry/normalized"),
            ue.rawText,
            Literal("Older producer text"),
        )
    )
    taxonomy_graph.add(
        (
            URIRef("https://example.org/entry/normalized"),
            skos.closeMatch,
            normalized_concept,
        )
    )
    taxonomy_graph.add((normalized_concept, RDF.type, ue.Intervention))

    monkeypatch.setattr(load_fuseki, "_clear_stamp_predicate", lambda *_args: None)

    def capture_query(_url, query):
        queries.append(query)
        if "?study aea:intervention ?itextRaw" in query:
            return [(str(row.study), str(row.ancestor)) for row in dataset.query(query)]
        return []

    monkeypatch.setattr(load_fuseki, "_select_pairs", capture_query)
    monkeypatch.setattr(
        load_fuseki,
        "_insert_data_batched",
        lambda _url, _graph, _predicate, pairs: inserted.extend(pairs) or len(pairs),
    )

    load_fuseki.stamp_concept_matches("http://fuseki:3030/ue/data")

    intervention_query = next(
        query for query in queries if "?study aea:intervention ?itextRaw" in query
    )
    assert "?entry ue:rawText ?itextRaw" in intervention_query
    assert "?entry ue:rawText ?itextNormalized" in intervention_query
    assert "AS ?itextNormalized" in intervention_query
    assert set(inserted) == {
        (str(exact_study), str(exact_concept)),
        (str(normalized_study), str(normalized_concept)),
    }


def test_compose_skip_existing_migrates_unversioned_raw_graphs_safely(
    monkeypatch, tmp_path
):
    taxonomy = tmp_path / "states.ttl"
    aea = tmp_path / "aea.ttl"
    who = tmp_path / "who.ttl"
    taxonomy.write_text("@prefix ex: <https://example.org/> . ex:a ex:p ex:b .\n")
    aea.write_text("@prefix ex: <https://example.org/> . ex:a ex:p ex:study .\n")
    who.write_text("@prefix ex: <https://example.org/> . ex:w ex:p ex:study .\n")
    manifest_path = tmp_path / "manifests" / "graph-versions.json"
    loaded: list[tuple[object, str]] = []

    monkeypatch.setattr(load_fuseki, "AEA_SOURCE", aea)
    monkeypatch.setattr(load_fuseki, "WHO_ICTRP_SOURCE", who)
    monkeypatch.setattr(load_fuseki, "DEFAULT_TAXONOMY_SOURCES", (taxonomy,))
    monkeypatch.setattr(load_fuseki, "graph_has_triples", lambda *_args: True)
    monkeypatch.setattr(
        load_fuseki,
        "put_named_graph",
        lambda path, graph_uri, _base: loaded.append((path, graph_uri)),
    )
    monkeypatch.setattr(load_fuseki, "merge_turtle_sources", lambda _paths: b"ttl")
    monkeypatch.setattr(load_fuseki, "put_named_graph_payload", lambda *_args: None)
    monkeypatch.setattr(load_fuseki, "stamp_concept_matches", lambda *_args: None)

    load_fuseki.load_default_graphs(
        skip_existing_raw=True,
        version_manifest_path=manifest_path,
    )

    assert [path for path, _graph_uri in loaded] == [aea, who]
    manifest = load_fuseki.read_version_manifest(manifest_path)
    assert manifest is not None
    assert len(manifest["taxonomyVersion"]) == 64
    assert len(manifest["datasetVersion"]) == 64
    assert manifest["dataset"]["missing"] == []
    assert {record["sha256"] for record in manifest["dataset"]["inputs"]} == {
        load_fuseki._input_record(aea)["sha256"],
        load_fuseki._input_record(who)["sha256"],
    }
