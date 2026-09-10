from pathlib import Path
import xml.etree.ElementTree as ET
from urllib.robotparser import RobotFileParser

from rdflib import Graph, Literal, Namespace
from rdflib.namespace import RDF, SKOS, OWL
from scripts.generate_discovery import ROOT, ORIGIN, XMLNS, generate, public_urls


def test_actual_release_coverage_and_robots(tmp_path):
    count = generate(output=tmp_path)
    tree = ET.parse(tmp_path / "sitemap.xml")
    urls = [node.text for node in tree.findall(f"{{{XMLNS}}}url/{{{XMLNS}}}loc")]
    assert count == len(urls) == len(set(urls))
    assert count > 9900
    assert ORIGIN + "/vocab/states/Malaria" in urls
    assert ORIGIN + "/ontology" in urls
    assert all(url.startswith(ORIGIN + "/") and "?" not in url for url in urls)
    assert not any("/api/" in url or "/subjects/" in url or "/labs/" in url for url in urls)
    assert not any(url.endswith("/AdmissionsAccess") for url in urls)
    robots = (tmp_path / "robots.txt").read_text()
    assert "<html" not in robots.lower()
    parser = RobotFileParser(); parser.parse(robots.splitlines())
    assert parser.can_fetch("Googlebot", ORIGIN + "/vocab/states/Malaria")
    assert parser.site_maps() == [ORIGIN + "/sitemap.xml"]
    first = (tmp_path / "sitemap.xml").read_bytes()
    generate(output=tmp_path)
    assert first == (tmp_path / "sitemap.xml").read_bytes()


def test_only_defined_public_terms_and_regeneration(tmp_path):
    folder = tmp_path / "vocabularies"; folder.mkdir()
    for name in ("states", "interventions", "regions", "sources"):
        (folder / f"{name}.ttl").write_text("")
    n = Namespace(ORIGIN + "/vocab/states/")
    graph = Graph()
    for term in (n.Current, n.Retired, n.Organizer):
        graph.add((term, RDF.type, SKOS.Collection if term == n.Organizer else SKOS.Concept))
    graph.add((n.Retired, OWL.deprecated, Literal(True)))
    graph.add((n.Current, SKOS.related, n.Missing))
    graph.add((Namespace("https://elsewhere.example/").External, RDF.type, SKOS.Concept))
    graph.serialize(folder / "states.ttl", format="turtle")
    urls = public_urls(tmp_path)
    assert str(n.Current) in urls and str(n.Organizer) in urls
    assert str(n.Retired) not in urls and str(n.Missing) not in urls
    assert not any("elsewhere" in url for url in urls)
    graph.add((n.Added, RDF.type, SKOS.Concept))
    graph.serialize(folder / "states.ttl", format="turtle")
    assert str(n.Added) in public_urls(tmp_path)


def test_generator_tracks_supported_public_routes():
    from api.routes.vocab import VOCABULARIES as routes
    from scripts.generate_discovery import VOCABULARIES
    assert set(VOCABULARIES) == set(routes)
    for name, config in routes.items():
        assert config.namespace == f"{ORIGIN}/vocab/{name}/"
