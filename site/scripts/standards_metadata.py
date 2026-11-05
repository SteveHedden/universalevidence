"""Generate collection metadata from the standards page's visible resource links."""
import html
import json
import re
from html.parser import HTMLParser

BASE = 'https://universalevidence.com'
URL = BASE + '/open-standards.html'
TITLE = 'Open standards and data sources for development & impact | Universal Evidence'
DESCRIPTION = ('Explore ontologies, vocabularies, measurement standards, and evidence sources '
               'Universal Evidence uses or is exploring for sustainable development and impact investing.')


class Resources(HTMLParser):
    def __init__(self):
        super().__init__()
        self.items = {}
        self.current = None
        self.in_description = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'a':
            url = attrs.get('href', '')
            if url.startswith('https://openknowledgegraphs.com/resource/') or url.startswith('https://openknowledgegraphs.com/?q='):
                self.current = {'url': url, 'name': [], 'description': []}
        if self.current is not None and tag == 'span':
            self.in_description = True

    def handle_data(self, data):
        if self.current is not None:
            self.current['description' if self.in_description else 'name'].append(data)

    def handle_endtag(self, tag):
        if tag == 'span':
            self.in_description = False
        if tag == 'a' and self.current is not None:
            item = self.current
            name = ' '.join(''.join(item['name']).split())
            description = ' '.join(''.join(item['description']).split())
            resource = {'@type': 'Thing', 'name': name, 'url': item['url']}
            if description:
                resource['description'] = description
            # Catalog search URLs are discovery links, never sameAs identities.
            self.items.setdefault(item['url'], resource)
            self.current = None


def enrich(document):
    parser = Resources()
    parser.feed(document.split('<body', 1)[1])
    items = list(parser.items.values())
    assert items, 'No visible resources found'
    publisher = BASE + '/#organization'
    website = BASE + '/#website'
    collection = URL + '#resources'
    graph = [
        {'@type': 'Organization', '@id': publisher, 'name': 'Universal Evidence',
         'url': BASE + '/', 'logo': {'@type': 'ImageObject',
         'url': BASE + '/assets/ue-mark-transparent.svg'}},
        {'@type': 'WebSite', '@id': website, 'url': BASE + '/',
         'name': 'Universal Evidence', 'publisher': {'@id': publisher}, 'inLanguage': 'en'},
        {'@type': 'CollectionPage', '@id': URL + '#webpage', 'url': URL,
         'name': 'Open standards and data sources', 'description': DESCRIPTION,
         'isPartOf': {'@id': website}, 'publisher': {'@id': publisher},
         'inLanguage': 'en', 'isAccessibleForFree': True,
         'datePublished': '2026-11-01',
         'mainEntity': {'@id': collection},
         'about': [{'@type': 'Thing', 'name': name} for name in
                   ['Sustainable development', 'Impact investing', 'Ontologies',
                    'Controlled vocabularies', 'Open standards', 'Evidence synthesis']],
         'keywords': ['sustainable development', 'impact measurement', 'ontologies',
                      'controlled vocabularies', 'data standards', 'research evidence'],
         'mentions': items},
        {'@type': 'ItemList', '@id': collection,
         'name': 'Ontologies, vocabularies, standards, and evidence sources',
         'description': 'Resources used, referenced, or being explored by Universal Evidence; roadmap sources are not yet integrated.',
         'itemListOrder': 'https://schema.org/ItemListUnordered',
         'numberOfItems': len(items),
         'itemListElement': [{'@type': 'ListItem', 'position': i, 'item': item}
                             for i, item in enumerate(items, 1)]},
    ]
    document = re.sub(r'<title>.*?</title>', '<title>' + html.escape(TITLE) + '</title>', document, count=1)
    for attr, key, value in [
        ('name', 'description', DESCRIPTION), ('property', 'og:title', TITLE),
        ('property', 'og:description', DESCRIPTION), ('name', 'twitter:title', TITLE),
        ('name', 'twitter:description', DESCRIPTION),
        ('name', 'robots', 'index,follow,max-image-preview:large'),
        ('property', 'og:image:alt', 'Universal Evidence: Know what’s been done. Decide what’s next.'),
        ('name', 'twitter:image:alt', 'Universal Evidence: Know what’s been done. Decide what’s next.'),
    ]:
        document = re.sub(r'<meta ' + attr + '="' + re.escape(key) + r'"[^>]*>', '', document)
        document = document.replace('</head>', '<meta ' + attr + '="' + key + '" content="' + html.escape(value, quote=True) + '"></head>')
    document = re.sub(r'<script id="standards-structured-data" type="application/ld\+json">.*?</script>', '', document, flags=re.S)
    payload = json.dumps({'@context': 'https://schema.org', '@graph': graph}, ensure_ascii=False, separators=(',', ':')).replace('<', '\\u003c')
    return document.replace('</head>', '<script id="standards-structured-data" type="application/ld+json">' + payload + '</script></head>')
