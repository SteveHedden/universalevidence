"""Application compatibility for retired vocabulary identifiers."""

import json
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def vocabulary_replacements() -> dict[str, str]:
    path = Path(__file__).parent / "resources" / "vocabulary-replacements.json"
    replacements = json.loads(path.read_text())
    for old, new in replacements.items():
        if (
            not isinstance(new, str)
            or not old.startswith("https://universalevidence.com/vocab/")
            or old.rsplit("/", 1)[0] != new.rsplit("/", 1)[0]
            or new in replacements
        ):
            raise ValueError("Vocabulary replacements must point directly to a current identifier in the same vocabulary")
    migrations = indicator_migrations()
    if set(migrations) & set(replacements):
        raise ValueError("Overlapping vocabulary migrations")
    replacements = {old: migrations.get(new, new) for old, new in replacements.items()}
    return {**replacements, **migrations}


def canonical_vocabulary_uri(uri: str) -> str:
    return vocabulary_replacements().get(uri, uri)


@lru_cache(maxsize=1)
def indicator_migrations() -> dict[str, str]:
    path = Path(__file__).parent / "resources" / "indicator-migrations.json"
    mapping = json.loads(path.read_text())
    old_prefix = "https://universalevidence.com/vocab/states/"
    new_prefix = "https://universalevidence.com/vocab/indicators/"
    for old, new in mapping.items():
        if not old.startswith(old_prefix) or new != new_prefix + old[len(old_prefix):]:
            raise ValueError("Invalid indicator identity migration")
    return mapping


def canonicalize_payload(value):
    """Translate exact identifiers, including dictionary keys; preserve all other data."""
    if isinstance(value, str):
        return canonical_vocabulary_uri(value)
    if isinstance(value, list):
        return [canonicalize_payload(v) for v in value]
    if isinstance(value, dict):
        return {canonical_vocabulary_uri(k): canonicalize_payload(v) for k, v in value.items()}
    return value


def canonicalize_indicator_graph(graph):
    """Normalize an in-memory historical asset without modifying its release bytes."""
    from rdflib import URIRef
    from rdflib.namespace import RDF, SKOS
    mapping = {URIRef(k): URIRef(v) for k, v in indicator_migrations().items()}
    for old, new in mapping.items():
        triples = set(graph.triples((old, None, None))) | set(graph.triples((None, None, old)))
        for triple in triples:
            graph.remove(triple)
            graph.add(tuple(mapping.get(x, x) if isinstance(x, URIRef) else x for x in triple))
        old_scheme = URIRef("https://universalevidence.com/vocab/states/")
        new_scheme = URIRef("https://universalevidence.com/vocab/indicators/")
        if (new, SKOS.inScheme, old_scheme) in graph:
            graph.remove((new, SKOS.inScheme, old_scheme))
            graph.add((new, SKOS.inScheme, new_scheme))
            graph.add((new_scheme, RDF.type, SKOS.ConceptScheme))
    return graph
