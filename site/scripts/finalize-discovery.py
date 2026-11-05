"""Finish discovery after all static pages are built."""
from pathlib import Path
import sys
import xml.etree.ElementTree as ET
root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(root))
from scripts.refresh_sitemap import refresh, NS
path = root / 'site/dist/sitemap.xml'
tree = ET.fromstring(path.read_text())
known = {n.text for n in tree.findall(f'{{{NS}}}url/{{{NS}}}loc')}
for route in ('/about.html', '/explore.html', '/open-standards.html', '/journal/childhood-stunting.html', '/browse', '/browse/states', '/browse/interventions', '/browse/states?view=all', '/browse/interventions?view=all'):
    url = 'https://universalevidence.com' + route
    if url not in known:
        ET.SubElement(ET.SubElement(tree, f'{{{NS}}}url'), f'{{{NS}}}loc').text = url
path.write_text(refresh(ET.tostring(tree, encoding='unicode')))
