"""Refresh vocabulary coverage while retaining existing editorial sitemap entries.

Run against the deployed vocabulary snapshot:
  python -m scripts.refresh_sitemap existing.xml refreshed.xml
"""
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from api.routes.vocab import VOCABULARIES, _load_graph, _browse_terms
from api.vocabulary_compat import indicator_migrations

NS = 'http://www.sitemaps.org/schemas/sitemap/0.9'

def refresh(existing):
    ET.register_namespace('', NS)
    root = ET.fromstring(existing)
    for entry in list(root):
        loc = entry.find(f'{{{NS}}}loc')
        if loc is not None and (loc.text in indicator_migrations() or (loc.text or '').startswith(('https://universalevidence.com/vocab/indicators/', 'https://universalevidence.com/browse/indicators'))):
            root.remove(entry)
    known = {node.text for node in root.findall(f'{{{NS}}}url/{{{NS}}}loc')}
    wanted = set()
    for name in ('states', 'interventions'):
        config = VOCABULARIES[name]
        wanted.update(map(str, _browse_terms(config, _load_graph(config))))
    for url in sorted(wanted - known):
        ET.SubElement(ET.SubElement(root, f'{{{NS}}}url'), f'{{{NS}}}loc').text = url
    ET.indent(root)
    return ET.tostring(root, encoding='utf-8', xml_declaration=True).decode() + '\n'

if __name__ == '__main__':
    Path(sys.argv[2]).write_text(refresh(Path(sys.argv[1]).read_text()))
