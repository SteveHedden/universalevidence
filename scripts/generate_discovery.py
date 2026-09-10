"""Generate crawler discovery files from the checked-out public vocabularies."""
from pathlib import Path
from urllib.parse import urlsplit
import argparse
import xml.etree.ElementTree as ET

from rdflib import Graph, URIRef
from rdflib.namespace import OWL, RDF, RDFS, SKOS, DCAT

ROOT = Path(__file__).resolve().parents[1]
ORIGIN = "https://universalevidence.com"
XMLNS = "http://www.sitemaps.org/schemas/sitemap/0.9"
# These are browser-readable routes, not RDF-only vocabulary listings.
PAGES = ("/", "/ontology", "/how-to-use-universal-evidence.html")
VOCABULARIES = ("states", "interventions", "regions", "sources")


def public_urls(root=ROOT):
    urls = {ORIGIN + path for path in PAGES}
    for name in VOCABULARIES:
        graph = Graph().parse(root / "vocabularies" / f"{name}.ttl", format="turtle")
        namespace = f"{ORIGIN}/vocab/{name}/"
        types = (DCAT.Dataset,) if name == "sources" else (SKOS.Concept, SKOS.Collection)
        subjects = set().union(*(set(graph.subjects(RDF.type, kind)) for kind in types))
        for subject in subjects:
            if not isinstance(subject, URIRef) or not str(subject).startswith(namespace):
                continue
            if any(str(flag).lower() in ("true", "1") for flag in graph.objects(subject, OWL.deprecated)):
                continue
            url = str(subject)
            term = url[len(namespace):]
            if not term or "/" in term or urlsplit(url).query or urlsplit(url).fragment:
                continue
            if any(char.isspace() for char in url):
                raise ValueError(f"Invalid public term URL: {url}")
            urls.add(url)
    return sorted(urls)


def generate(root=ROOT, output=None):
    output = output or root / "site" / "public"
    urls = public_urls(root)
    if len(urls) > 50000:
        raise ValueError("Sitemap exceeds 50,000 URLs; split into a sitemap index before release")
    ET.register_namespace("", XMLNS)
    tree = ET.Element(f"{{{XMLNS}}}urlset")
    for url in urls:
        entry = ET.SubElement(tree, f"{{{XMLNS}}}url")
        ET.SubElement(entry, f"{{{XMLNS}}}loc").text = url
    ET.indent(tree)
    xml = ET.tostring(tree, encoding="utf-8", xml_declaration=True) + b"\n"
    if len(xml) > 50 * 1024 * 1024:
        raise ValueError("Sitemap exceeds 50 MiB")
    robots = ("# Cloudflare may prepend managed bot policies at the edge.\n"
              "User-agent: *\nAllow: /\n\n"
              f"Sitemap: {ORIGIN}/sitemap.xml\n")
    output.mkdir(parents=True, exist_ok=True)
    for name, data in (("sitemap.xml", xml), ("robots.txt", robots.encode())):
        temporary = output / (name + ".tmp")
        temporary.write_bytes(data)
        temporary.replace(output / name)
    return len(urls)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    print(f"Generated robots.txt and sitemap.xml with {generate(output=args.output):,} URLs")
