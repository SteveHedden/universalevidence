"""Vocabulary and ontology API routes."""

from __future__ import annotations

import json
from dataclasses import dataclass
from html import escape
from pathlib import Path
from typing import Iterable
from urllib.parse import quote, urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, RedirectResponse
from rdflib import BNode, Graph, Literal, URIRef
from rdflib.namespace import DCAT, DCTERMS, RDF, RDFS, SKOS
from api.vocabulary_compat import vocabulary_replacements

REPO_ROOT = Path(__file__).resolve().parents[2]

JSON = "application/json"
JSON_LD = "application/ld+json"
TURTLE = "text/turtle"
HTML = "text/html"
SUPPORTED_MEDIA_TYPES = {TURTLE, JSON_LD, JSON}

UE = URIRef("https://universalevidence.com/ontology/")
UE_STATE = URIRef("https://universalevidence.com/ontology/State")
UE_INTERVENTION = URIRef("https://universalevidence.com/ontology/Intervention")
OWL_CLASS = URIRef("http://www.w3.org/2002/07/owl#Class")
OWL_OBJECT_PROPERTY = URIRef("http://www.w3.org/2002/07/owl#ObjectProperty")
OWL_DATATYPE_PROPERTY = URIRef("http://www.w3.org/2002/07/owl#DatatypeProperty")


@dataclass(frozen=True)
class ResourceConfig:
    name: str
    path: Path
    namespace: str


VOCABULARIES = {
    "states": ResourceConfig(
        name="states",
        path=REPO_ROOT / "vocabularies" / "states.ttl",
        namespace="https://universalevidence.com/vocab/states/",
    ),
    "interventions": ResourceConfig(
        name="interventions",
        path=REPO_ROOT / "vocabularies" / "interventions.ttl",
        namespace="https://universalevidence.com/vocab/interventions/",
    ),
    "regions": ResourceConfig(
        name="regions",
        path=REPO_ROOT / "vocabularies" / "regions.ttl",
        namespace="https://universalevidence.com/vocab/regions/",
    ),
    "sources": ResourceConfig(
        name="sources",
        path=REPO_ROOT / "vocabularies" / "sources.ttl",
        namespace="https://universalevidence.com/vocab/sources/",
    ),
}

VOCABULARY_DISPLAY_NAMES = {
    "states": "Conditions & outcomes",
    "interventions": "Interventions",
    "regions": "Regions",
    "sources": "Evidence sources",
}

HIERARCHICAL_VOCABULARIES = {"states", "interventions", "regions"}
DEFINITION_VOCABULARIES = {"states", "interventions"}
GEONAMES_COUNTRY_LABELS_PATH = REPO_ROOT / "api" / "resources" / "geonames-country-labels.json"
SAFE_EXTERNAL_SCHEMES = {"http", "https"}
COPYABLE_TERM_TYPES = {
    "states": UE_STATE,
    "interventions": UE_INTERVENTION,
}

ONTOLOGY = ResourceConfig(
    name="ontology",
    path=REPO_ROOT / "ontology" / "ue.ttl",
    namespace="https://universalevidence.com/ontology/",
)

router = APIRouter()


def negotiate_content_type(request: Request, allow_html: bool = False) -> str | None:
    """Choose a supported representation from the Accept header.

    `allow_html` is only set by the /vocab/{vocabulary}/{term} route --
    other routes (ontology, vocabulary listings) have no HTML stub and
    should keep 406-ing text/html.
    """
    accept = request.headers.get("accept")
    if not accept:
        return TURTLE

    parsed = []
    for index, raw_part in enumerate(accept.split(",")):
        media_range, *params = [part.strip() for part in raw_part.split(";")]
        if not media_range:
            continue
        q = 1.0
        for param in params:
            if param.startswith("q="):
                try:
                    q = float(param.removeprefix("q="))
                except ValueError:
                    q = 0.0
        if q > 0:
            parsed.append((media_range.lower(), q, index))

    if not parsed:
        return TURTLE

    parsed.sort(key=lambda item: (-item[1], item[2]))
    for media_range, _q, _index in parsed:
        if media_range in SUPPORTED_MEDIA_TYPES:
            return media_range
        if media_range in {"*/*", "application/*", "text/*"}:
            return TURTLE
        if media_range == HTML:
            return HTML if allow_html else None
    return None


def _not_acceptable() -> JSONResponse:
    return JSONResponse(status_code=406, content={"error": "Not acceptable"})


def _missing_vocabulary(name: str) -> JSONResponse:
    return JSONResponse(
        status_code=404,
        content={"error": "Vocabulary not available", "vocabulary": name},
    )


def _missing_term(name: str, term: str) -> JSONResponse:
    return JSONResponse(
        status_code=404,
        content={"error": "Term not found", "vocabulary": name, "term": term},
    )


def _load_graph(config: ResourceConfig) -> Graph | None:
    if not config.path.exists():
        return None
    graph = Graph()
    graph.parse(config.path, format="turtle")
    return graph


def _literal(graph: Graph, subject: URIRef, predicates: Iterable[URIRef]) -> str | None:
    for predicate in predicates:
        value = graph.value(subject, predicate)
        if value is not None:
            return str(value)
    return None


def _values(graph: Graph, subject: URIRef, predicate: URIRef) -> list[str]:
    return [str(value) for value in graph.objects(subject, predicate)]


def _term_subjects(graph: Graph, namespace: str) -> list[URIRef]:
    namespace_root = URIRef(namespace)
    subjects = {
        subject
        for subject in graph.subjects()
        if isinstance(subject, URIRef)
        and str(subject).startswith(namespace)
        and subject != namespace_root
    }
    return sorted(subjects, key=str)


def _copy_subject(graph: Graph, subject: URIRef) -> Graph:
    term_graph = Graph()
    for prefix, namespace in graph.namespaces():
        term_graph.bind(prefix, namespace)

    pending = [subject]
    copied = set()
    while pending:
        current = pending.pop()
        if current in copied:
            continue
        copied.add(current)
        for triple in graph.triples((current, None, None)):
            term_graph.add(triple)
            obj = triple[2]
            if isinstance(obj, BNode) and obj not in copied:
                pending.append(obj)
    return term_graph


def _json_value(value) -> str | int | float | bool:
    if isinstance(value, Literal):
        return value.toPython()
    return str(value)


def _source_record(graph: Graph, subject: URIRef) -> dict:
    record = {
        "uri": str(subject),
        "type": [str(value) for value in graph.objects(subject, RDF.type)],
    }
    label = _literal(graph, subject, (RDFS.label, SKOS.prefLabel))
    title = _literal(graph, subject, (DCTERMS.title,))
    description = _literal(graph, subject, (DCTERMS.description, RDFS.comment))
    homepage = _literal(graph, subject, (DCAT.landingPage,))
    license_uri = _literal(graph, subject, (DCTERMS.license,))
    if label:
        record["label"] = label
    if title:
        record["title"] = title
    if description:
        record["description"] = description
    if homepage:
        record["homepage"] = homepage
    if license_uri:
        record["license"] = license_uri
    return record


def _taxonomy_record(graph: Graph, subject: URIRef) -> dict:
    record = {"uri": str(subject)}
    label = _literal(graph, subject, (RDFS.label, SKOS.prefLabel, DCTERMS.title))
    definition = _literal(graph, subject, (SKOS.definition, RDFS.comment, DCTERMS.description))
    broader = _values(graph, subject, SKOS.broader)
    if label:
        record["label"] = label
    if definition:
        record["definition"] = definition
    if broader:
        record["broader"] = broader
    return record


def _ontology_record(graph: Graph) -> dict:
    classes = sorted(str(subject) for subject in graph.subjects(RDF.type, OWL_CLASS))
    properties = sorted(
        {
            str(subject)
            for rdf_type in (OWL_OBJECT_PROPERTY, OWL_DATATYPE_PROPERTY, RDF.Property)
            for subject in graph.subjects(RDF.type, rdf_type)
        }
    )
    return {
        "uri": str(UE),
        "title": _literal(graph, UE, (DCTERMS.title, RDFS.label)),
        "description": _literal(graph, UE, (DCTERMS.description, RDFS.comment)),
        "classes": classes,
        "properties": properties,
    }


def _json_response(config: ResourceConfig, graph: Graph, subject: URIRef | None = None) -> JSONResponse:
    if config.name == "ontology":
        return JSONResponse(content=_ontology_record(graph), headers={"Vary": "Accept"})

    if subject is not None:
        record = _source_record(graph, subject) if config.name == "sources" else _taxonomy_record(graph, subject)
        return JSONResponse(content=record, headers={"Vary": "Accept"})

    terms = [
        _source_record(graph, term) if config.name == "sources" else _taxonomy_record(graph, term)
        for term in _term_subjects(graph, config.namespace)
    ]
    return JSONResponse(
        content={"vocabulary": config.name, "terms": terms},
        headers={"Vary": "Accept"},
    )


def _rdf_response(graph: Graph, media_type: str) -> Response:
    rdf_format = "json-ld" if media_type == JSON_LD else "turtle"
    body = graph.serialize(format=rdf_format)
    return Response(content=body, media_type=media_type, headers={"Vary": "Accept"})


def _load_geonames_country_labels() -> dict[str, str]:
    """Load the tracked presentation snapshot required by region term pages."""
    try:
        payload = json.loads(GEONAMES_COUNTRY_LABELS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("Packaged GeoNames country labels are unavailable") from exc

    metadata = payload.get("_meta")
    labels = payload.get("labels")
    if not isinstance(metadata, dict) or not isinstance(labels, dict):
        raise RuntimeError("Packaged GeoNames country labels are malformed")
    if metadata.get("label_count") != len(labels):
        raise RuntimeError("Packaged GeoNames country label count does not match metadata")

    normalized = {}
    for uri, label in labels.items():
        if (
            not isinstance(uri, str)
            or not uri.startswith("https://sws.geonames.org/")
            or not isinstance(label, str)
            or not label.strip()
        ):
            raise RuntimeError("Packaged GeoNames country labels contain an invalid entry")
        normalized[uri] = label
    return normalized


GEONAMES_COUNTRY_LABELS = _load_geonames_country_labels()


def _relation_label(graph: Graph, uri: str) -> str:
    label = _literal(graph, URIRef(uri), (SKOS.prefLabel, RDFS.label, DCTERMS.title))
    if label:
        return label

    parsed = urlsplit(uri)
    if parsed.hostname == "sws.geonames.org":
        country_label = GEONAMES_COUNTRY_LABELS.get(uri)
        if country_label:
            return country_label
        identifier = parsed.path.strip("/").rsplit("/", 1)[-1]
        if identifier:
            return f"GeoNames place {identifier}"
    return uri


def _safe_external_href(uri: str) -> str | None:
    parsed = urlsplit(uri)
    if parsed.scheme.lower() in SAFE_EXTERNAL_SCHEMES and parsed.netloc:
        return uri
    return None


def _term_link(config: ResourceConfig, graph: Graph, uri: str) -> str:
    label = _relation_label(graph, uri)
    is_internal = uri.startswith(config.namespace)
    if is_internal:
        local_name = uri.removeprefix(config.namespace)
        href = f"/vocab/{config.name}/{quote(local_name, safe='')}"
        external_attribute = ""
        arrow = "→"
        external_text = ""
    else:
        safe_href = _safe_external_href(uri)
        if safe_href is None:
            return (
                '<li class="concept-item">'
                '<span class="concept-link concept-link-static">'
                f"<span>{escape(label)}</span>"
                "</span></li>"
            )
        href = safe_href
        external_attribute = ' rel="external"'
        arrow = "↗"
        external_text = '<span class="sr-only"> (external)</span>'
    return (
        '<li class="concept-item">'
        f'<a class="concept-link" href="{escape(href, quote=True)}"{external_attribute}>'
        f"<span>{escape(label)}{external_text}</span>"
        f'<span class="concept-arrow" aria-hidden="true">{arrow}</span>'
        "</a></li>"
    )


def _relation_list(config: ResourceConfig, graph: Graph, uris: Iterable[str]) -> str:
    def sort_key(uri: str) -> tuple[str, str]:
        label = _relation_label(graph, uri)
        return label.casefold(), uri

    unique_uris = set(uris)
    return "\n".join(_term_link(config, graph, uri) for uri in sorted(unique_uris, key=sort_key))


def _narrower_values(graph: Graph, subject: URIRef) -> set[str]:
    explicit = _values(graph, subject, SKOS.narrower)
    inverse = (str(candidate) for candidate in graph.subjects(SKOS.broader, subject))
    return set(explicit).union(inverse)


def _meta_description(label: str, vocabulary_label: str, description: str | None) -> str:
    fallback = f"{label} in the Universal Evidence {vocabulary_label.lower()} vocabulary."
    value = " ".join((description or fallback).split())
    if len(value) <= 180:
        return value
    shortened = value[:177].rsplit(" ", 1)[0]
    if not shortened:
        shortened = value[:177]
    return f"{shortened}…"


def _relation_section(section_id: str, title: str, items: str) -> str:
    if not items:
        return ""
    return f"""<section class="term-section" aria-labelledby="{section_id}">
<h2 id="{section_id}">{title}</h2>
<ul class="concept-grid" role="list">
{items}
</ul>
</section>"""


def _source_details(graph: Graph, subject: URIRef, label: str) -> str:
    homepage = _literal(graph, subject, (DCAT.landingPage,))
    license_uri = _literal(graph, subject, (DCTERMS.license,))
    details = []
    if homepage:
        homepage_label = f"Visit {label} homepage"
        safe_homepage = _safe_external_href(homepage)
        homepage_html = (
            f'<a href="{escape(safe_homepage, quote=True)}">{escape(homepage_label)}</a>'
            if safe_homepage
            else f'<span class="source-link-static">{escape(homepage_label)}</span>'
        )
        details.append(
            "<div>"
            "<dt>Homepage</dt>"
            f"<dd>{homepage_html}</dd>"
            "</div>"
        )
    if license_uri:
        safe_license = _safe_external_href(license_uri)
        license_html = (
            f'<a href="{escape(safe_license, quote=True)}">View license terms</a>'
            if safe_license
            else '<span class="source-link-static">View license terms</span>'
        )
        details.append(
            "<div>"
            "<dt>License</dt>"
            f"<dd>{license_html}</dd>"
            "</div>"
        )
    if not details:
        return ""
    return f"""<section class="term-section" aria-labelledby="source-details-title">
<h2 id="source-details-title">Source details</h2>
<dl class="source-details">
{''.join(details)}
</dl>
</section>"""


TERM_PAGE_CSS = """
:root {
  color: #182027;
  background: #f6f7f9;
  font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  font-synthesis: none;
  line-height: 1.5;
  text-rendering: optimizeLegibility;
}
* { box-sizing: border-box; }
html { min-width: 0; }
body { margin: 0; min-width: 0; overflow-x: hidden; }
a { color: #1664a3; text-underline-offset: 0.16em; }
a:hover { color: #125488; }
a:focus-visible {
  border-radius: 4px;
  outline: 3px solid #1664a3;
  outline-offset: 3px;
}
.site-header { padding: 24px clamp(16px, 4vw, 32px); }
.header-inner { margin: 0 auto; max-width: 960px; }
.brand-link {
  align-items: center;
  color: #111820;
  display: inline-flex;
  gap: 13px;
  max-width: 100%;
  text-decoration: none;
}
.brand-link:hover { color: #111820; }
.brand-mark { display: block; flex: 0 0 auto; height: 44px; width: 44px; }
.brand-name { display: block; font-size: 1.5rem; font-weight: 700; line-height: 1.1; }
.tagline { color: #65717d; display: block; font-size: 0.85rem; font-style: italic; margin-top: 2px; }
.page-shell { padding: 0 clamp(12px, 4vw, 32px) 48px; }
.page-content { margin: 0 auto; max-width: 960px; min-width: 0; }
.breadcrumb {
  align-items: center;
  color: #65717d;
  display: flex;
  flex-wrap: wrap;
  font-size: 0.86rem;
  gap: 8px;
  margin: 0 0 14px;
}
.breadcrumb a { color: #4d5a66; }
.vocabulary-badge {
  background: #eef5fb;
  border: 1px solid #bed8ee;
  border-radius: 999px;
  color: #125488;
  display: inline-flex;
  font-size: 0.75rem;
  font-weight: 700;
  letter-spacing: 0.045em;
  line-height: 1.2;
  padding: 5px 10px;
  text-transform: uppercase;
}
.term-card {
  background: #ffffff;
  border: 1px solid #d9dee5;
  border-radius: 10px;
  box-shadow: 0 8px 28px rgb(24 32 39 / 7%);
  min-width: 0;
  overflow: hidden;
}
.term-heading { padding: clamp(22px, 5vw, 40px); }
h1, h2 { color: #111820; overflow-wrap: anywhere; }
h1 {
  font-size: clamp(2rem, 6vw, 3.25rem);
  letter-spacing: -0.025em;
  line-height: 1.05;
  margin: 14px 0 0;
}
h2 { font-size: 1.05rem; line-height: 1.25; margin: 0 0 12px; }
.uri-block {
  background: #f6f7f9;
  border-bottom: 1px solid #e3e7eb;
  border-top: 1px solid #e3e7eb;
  padding: 18px clamp(22px, 5vw, 40px);
}
.uri-heading {
  align-items: center;
  display: flex;
  gap: 12px;
  justify-content: space-between;
  margin-bottom: 7px;
}
.uri-label {
  color: #65717d;
  display: block;
  font-size: 0.75rem;
  font-weight: 700;
  letter-spacing: 0.05em;
  text-transform: uppercase;
}
.uri-block code {
  color: #35414d;
  display: block;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 0.83rem;
  overflow-wrap: anywhere;
  user-select: all;
  word-break: break-word;
}
.copy-uri-button {
  background: #ffffff;
  border: 1px solid #9bbbd4;
  border-radius: 6px;
  color: #125488;
  cursor: pointer;
  flex: 0 0 auto;
  font: inherit;
  font-size: 0.78rem;
  font-weight: 700;
  padding: 5px 9px;
}
.copy-uri-button:hover { background: #eef5fb; }
.copy-uri-button:focus-visible {
  outline: 3px solid rgb(22 100 163 / 28%);
  outline-offset: 2px;
}
.copy-uri-status {
  color: #52606d;
  font-size: 0.78rem;
  margin: 7px 0 0;
  min-height: 1.2em;
}
.term-body { display: grid; gap: 0; padding: 0 clamp(22px, 5vw, 40px); }
.term-section { border-bottom: 1px solid #e3e7eb; padding: 28px 0; }
.term-section:last-child { border-bottom: 0; }
.definition p {
  color: #35414d;
  font-size: 1.05rem;
  line-height: 1.7;
  margin: 0;
  overflow-wrap: anywhere;
}
.concept-grid {
  display: grid;
  gap: 10px;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  list-style: none;
  margin: 0;
  padding: 0;
}
.concept-item { min-width: 0; }
.sr-only {
  clip: rect(0 0 0 0);
  clip-path: inset(50%);
  height: 1px;
  overflow: hidden;
  position: absolute;
  white-space: nowrap;
  width: 1px;
}
.concept-link {
  align-items: center;
  background: #f8fafc;
  border: 1px solid #d9e2ea;
  border-radius: 8px;
  color: #174f7d;
  display: flex;
  font-weight: 650;
  gap: 12px;
  justify-content: space-between;
  min-height: 48px;
  overflow-wrap: anywhere;
  padding: 11px 14px;
  text-decoration: none;
}
.concept-link:hover { background: #eef5fb; border-color: #90bce3; color: #125488; }
.concept-link-static { color: #35414d; cursor: default; }
.concept-link-static:hover { background: #f8fafc; border-color: #d9e2ea; color: #35414d; }
.concept-arrow { flex: 0 0 auto; font-size: 1.1rem; }
.empty-relations { color: #65717d; font-style: italic; margin: 28px 0; }
.source-details { display: grid; gap: 14px; margin: 0; }
.source-details div {
  align-items: baseline;
  display: grid;
  gap: 6px 18px;
  grid-template-columns: 90px minmax(0, 1fr);
}
.source-details dt {
  color: #65717d;
  font-size: 0.8rem;
  font-weight: 700;
  text-transform: uppercase;
}
.source-details dd { margin: 0; min-width: 0; overflow-wrap: anywhere; }
.source-link-static { color: #35414d; }
.page-footer {
  color: #65717d;
  font-size: 0.78rem;
  margin: 18px auto 0;
  max-width: 960px;
  text-align: center;
}
@media (max-width: 600px) {
  .site-header { padding-bottom: 18px; }
  .brand-link { gap: 10px; }
  .brand-mark { height: 36px; width: 36px; }
  .brand-name { font-size: 1.25rem; }
  .tagline { font-size: 0.78rem; }
  .concept-grid { grid-template-columns: minmax(0, 1fr); }
  .source-details div { align-items: start; grid-template-columns: minmax(0, 1fr); }
}
""".strip()


def _html_response(config: ResourceConfig, graph: Graph, subject: URIRef, term: str) -> Response:
    """Branded, server-rendered, crawlable HTML representation for a term."""
    label = _literal(graph, subject, (SKOS.prefLabel, RDFS.label, DCTERMS.title)) or term
    definition = _literal(graph, subject, (SKOS.definition, RDFS.comment, DCTERMS.description))
    broader = _values(graph, subject, SKOS.broader)
    narrower = _narrower_values(graph, subject)

    vocabulary_label = VOCABULARY_DISPLAY_NAMES[config.name]
    canonical_uri = str(subject)
    description_meta = _meta_description(label, vocabulary_label, definition)

    definition_html = ""
    if definition and config.name in DEFINITION_VOCABULARIES:
        definition_html = f"""<section class="term-section definition" aria-labelledby="definition-title">
<h2 id="definition-title">Definition</h2>
<p>{escape(definition)}</p>
</section>"""
    elif definition and config.name == "sources":
        definition_html = f"""<section class="term-section definition" aria-labelledby="description-title">
<h2 id="description-title">About this source</h2>
<p>{escape(definition)}</p>
</section>"""

    relations_html = ""
    if config.name in HIERARCHICAL_VOCABULARIES:
        broader_html = _relation_list(config, graph, broader)
        narrower_html = _relation_list(config, graph, narrower)
        relations_html = "\n".join(
            section
            for section in (
                _relation_section("broader-title", "Broader concepts", broader_html),
                _relation_section("narrower-title", "Narrower concepts", narrower_html),
            )
            if section
        )
        if not relations_html:
            relations_html = '<p class="empty-relations">No broader or narrower concepts are recorded.</p>'

    source_details_html = _source_details(graph, subject, label) if config.name == "sources" else ""
    copyable_type = COPYABLE_TERM_TYPES.get(config.name)
    supports_copy = copyable_type is not None and (
        subject,
        RDF.type,
        copyable_type,
    ) in graph
    copy_button_html = (
        f'<button class="copy-uri-button" id="copy-uri" type="button" '
        f'data-uri="{escape(canonical_uri, quote=True)}" '
        'aria-describedby="copy-uri-status">Copy URI</button>'
        if supports_copy
        else ""
    )
    copy_status_html = (
        '<p class="copy-uri-status" id="copy-uri-status" role="status" '
        'aria-live="polite" aria-atomic="true"></p>'
        if supports_copy
        else ""
    )
    copy_script_html = """
<script>
(() => {
  const button = document.getElementById("copy-uri");
  const status = document.getElementById("copy-uri-status");
  if (!button || !status) return;
  button.addEventListener("click", async () => {
    try {
      if (!navigator.clipboard || typeof navigator.clipboard.writeText !== "function") {
        throw new Error("Clipboard unavailable");
      }
      await navigator.clipboard.writeText(button.dataset.uri);
      status.textContent = "URI copied.";
    } catch (_error) {
      status.textContent = "Could not copy URI. Select and copy it manually.";
    }
  });
})();
</script>""" if supports_copy else ""

    body = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(label)} — {escape(vocabulary_label)} — Universal Evidence</title>
<meta name="description" content="{escape(description_meta, quote=True)}">
<link rel="canonical" href="{escape(canonical_uri, quote=True)}">
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<style>{TERM_PAGE_CSS}</style>
</head>
<body>
<header class="site-header">
  <div class="header-inner">
    <a class="brand-link" href="/" aria-label="Universal Evidence home">
      <svg class="brand-mark" viewBox="44 16 112 112" aria-hidden="true" focusable="false">
        <defs><clipPath id="logo-clip"><circle cx="100" cy="72" r="56"></circle></clipPath></defs>
        <circle cx="100" cy="72" r="56" fill="#c9a547"></circle>
        <g clip-path="url(#logo-clip)">
          <rect x="43" y="72" width="114" height="6" fill="#f6f7f9"></rect>
          <rect x="43" y="86" width="114" height="6" fill="#f6f7f9"></rect>
          <rect x="43" y="100" width="114" height="6" fill="#f6f7f9"></rect>
          <rect x="43" y="114" width="114" height="6" fill="#f6f7f9"></rect>
        </g>
      </svg>
      <span>
        <span class="brand-name">Universal Evidence</span>
        <span class="tagline">what was tried and what happened</span>
      </span>
    </a>
  </div>
</header>
<main class="page-shell">
  <div class="page-content">
    <nav class="breadcrumb" aria-label="Breadcrumb">
      <a href="/">Home</a><span aria-hidden="true">/</span><span aria-current="page">{escape(vocabulary_label)}</span>
    </nav>
    <article class="term-card">
      <header class="term-heading">
        <span class="vocabulary-badge">{escape(vocabulary_label)}</span>
        <h1>{escape(label)}</h1>
      </header>
      <div class="uri-block">
        <div class="uri-heading">
          <span class="uri-label">Canonical URI</span>
          {copy_button_html}
        </div>
        <code id="canonical-uri">{escape(canonical_uri)}</code>
        {copy_status_html}
      </div>
      <div class="term-body">
        {definition_html}
        {relations_html}
        {source_details_html}
      </div>
    </article>
    <footer class="page-footer">Universal Evidence controlled vocabulary</footer>
  </div>
</main>
{copy_script_html}
</body>
</html>"""
    return Response(
        content=body,
        media_type=HTML,
        headers={"Cache-Control": "public, max-age=3600", "Vary": "Accept"},
    )


def _serve_resource(request: Request, config: ResourceConfig, term: str | None = None, allow_html: bool = False):
    media_type = negotiate_content_type(request, allow_html=allow_html)
    if media_type is None:
        return _not_acceptable()

    graph = _load_graph(config)
    if graph is None:
        return _missing_vocabulary(config.name)

    subject = None
    response_graph = graph
    if term is not None:
        subject = URIRef(f"{config.namespace}{term}")
        if (subject, None, None) not in graph:
            target = vocabulary_replacements().get(str(subject))
            if (
                isinstance(target, str)
                and target.startswith(config.namespace)
                and (URIRef(target), RDF.type, SKOS.Concept) in graph
            ):
                return RedirectResponse(
                    url=urlsplit(target).path,
                    status_code=308,
                    headers={"Cache-Control": "no-cache"},
                )
            return _missing_term(config.name, term)
        response_graph = _copy_subject(graph, subject)

    if media_type == HTML:
        return _html_response(config, graph, subject, term)
    if media_type == JSON:
        return _json_response(config, graph, subject)
    return _rdf_response(response_graph, media_type)


@router.get("/ontology")
async def ontology(request: Request):
    if negotiate_content_type(request, allow_html=True) == HTML:
        graph = _load_graph(ONTOLOGY)
        if graph is None:
            return _missing_vocabulary("ontology")
        title = escape(_literal(graph, UE, (DCTERMS.title, RDFS.label)) or "Universal Evidence Ontology")
        version = escape(str(graph.value(UE, URIRef("http://www.w3.org/2002/07/owl#versionInfo")) or ""))
        sections = []
        for subject in sorted(set(graph.subjects(RDFS.label, None)), key=str):
            if not isinstance(subject, URIRef) or not str(subject).startswith(str(UE)):
                continue
            term = str(subject)[len(str(UE)):]
            label = escape(_literal(graph, subject, (RDFS.label,)) or term)
            description = escape(_literal(graph, subject, (RDFS.comment,)) or "")
            sections.append(f'<section id="{escape(term, quote=True)}"><h2>{label}</h2><p><code>{escape(str(subject))}</code></p><p>{description}</p></section>')
        body = f'''<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{title}</title>
<style>body{{font:18px/1.6 system-ui,sans-serif;max-width:900px;margin:40px auto;padding:0 24px;color:#18232c}}section{{border-top:1px solid #ccd5dd;margin-top:32px}}code{{font-size:.8em;overflow-wrap:anywhere}}a{{color:#1664a3}}</style></head>
<body><main><h1>{title}</h1><p>Version {version} · <a href="https://creativecommons.org/licenses/by/4.0/">CC BY 4.0</a></p>
<p>Classes, properties and validation rules for Universal Evidence. Software can request this document as Turtle or JSON-LD using the Accept header.</p>
{''.join(sections)}</main></body></html>'''
        return Response(content=body, media_type=HTML, headers={"Vary": "Accept", "Cache-Control": "no-cache"})
    return _serve_resource(request, ONTOLOGY)


@router.get("/ontology/{term}")
async def ontology_term(request: Request, term: str):
    """Resolve known schema terms to the complete ontology document."""
    graph = _load_graph(ONTOLOGY)
    if graph is None:
        return _missing_vocabulary("ontology")
    subject = URIRef(f"{ONTOLOGY.namespace}{term}")
    if (subject, None, None) not in graph:
        return _missing_term("ontology", term)
    return RedirectResponse(url="/ontology/", status_code=303)


@router.get("/vocab/{vocabulary}")
async def vocabulary(request: Request, vocabulary: str):
    config = VOCABULARIES.get(vocabulary)
    if config is None:
        return _missing_vocabulary(vocabulary)
    return _serve_resource(request, config)


@router.get("/vocab/{vocabulary}/{term}")
async def vocabulary_term(request: Request, vocabulary: str, term: str):
    config = VOCABULARIES.get(vocabulary)
    if config is None:
        return _missing_vocabulary(vocabulary)
    return _serve_resource(request, config, term=term, allow_html=True)
