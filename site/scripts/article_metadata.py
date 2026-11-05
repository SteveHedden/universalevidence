"""Structured metadata for the published stunting article, derived from its content."""
import html
import json
import re
from html.parser import HTMLParser

BASE = 'https://universalevidence.com'
URL = BASE + '/journal/childhood-stunting.html'
DESCRIPTION = ('What does the evidence say about childhood stunting? Explore five intervention '
               'groups, research findings, and organizations putting related approaches into practice.')
# Editorial publication date: do not reset this on each build.
PUBLISHED = '2026-10-30'
MODIFIED = '2026-10-30'

# Labels must occur in the visible article. URLs identify organizations, not
# project documents or funders of Universal Evidence.
ORGANIZATIONS = {
    'Zvitambo Institute for Maternal and Child Health Research': 'https://www.zvitambo.com/',
    'Makerere University': 'https://www.mak.ac.ug/',
    'University of Copenhagen': 'https://www.ku.dk/en',
    'Action Against Hunger': 'https://www.actionagainsthunger.org/',
    'World Bank': 'https://www.worldbank.org/',
    'UNICEF': 'https://www.unicef.org/',
    'World Food Programme': 'https://www.wfp.org/',
    'GiveDirectly': 'https://www.givedirectly.org/',
    'Gates Foundation': 'https://www.gatesfoundation.org/',
    "Children's Investment Fund Foundation": 'https://ciff.org/',
    'World Vision': 'https://www.wvi.org/',
    'World Vision Korea': 'https://www.worldvision.or.kr/',
    'Johns Hopkins': 'https://www.jhu.edu/',
    'KOICA': 'https://www.koica.go.kr/sites/koica_en/index.do',
    'CERP': 'https://www.cerp.org.pk/',
    'Shahid Hussain Foundation': 'https://shf.org.pk/about/',
    'Save the Children': 'https://www.savethechildren.net/',
    'Mediae': 'https://mediae.org/',
    'The Power of Nutrition': 'https://www.powerofnutrition.org/',
    'Kidogo': 'https://www.linkedin.com/company/kidogo-early-years',
    'Mulago Foundation': 'https://www.mulagofoundation.org/',
    'WSUP': 'https://www.linkedin.com/company/water-sanitation-for-urban-populations',
    'Sanivation': 'https://sanivation.com/',
    'FINCA Ventures': 'https://finca.org/fincaventures',
    'PIDG': 'https://pidg.org/',
    'Common Fund for Commodities': 'https://www.common-fund.org/',
    'Incofin': 'https://www.linkedin.com/company/incofin-investment-management',
    'GAIN': 'https://www.gainhealth.org/',
    'Africa Improved Foods': 'https://africaimprovedfoods.com/',
    'IFC': 'https://www.ifc.org/en/home',
    'FMO': 'https://www.fmo.nl/',
}


class Content(HTMLParser):
    def __init__(self):
        super().__init__()
        self.text = []
        self.links = []
        self.current = None

    def handle_starttag(self, tag, attrs):
        if tag == 'a':
            self.current = [dict(attrs).get('href', ''), []]

    def handle_data(self, data):
        self.text.append(data)
        if self.current is not None:
            self.current[1].append(data)

    def handle_endtag(self, tag):
        if tag == 'a' and self.current is not None:
            self.links.append((self.current[0], ''.join(self.current[1]).strip()))
            self.current = None


def enrich(document, title, body, source_links):
    content = Content()
    content.feed(body)
    visible = ' '.join(content.text)
    topics = {BASE + '/vocab/states/Stunting': 'Childhood stunting'}
    for url, label in content.links:
        if url.startswith(BASE + '/vocab/') and label:
            topics.setdefault(url, label)
    mentions = [
        {'@type': 'Organization', '@id': url, 'name': name, 'url': url}
        for name, url in ORGANIZATIONS.items() if name in visible
    ]
    # These names are documented in the article through project sources;
    # don't treat those source-document URLs as organization identities.
    mentions += [{'@type': 'Organization', 'name': name}
                 for name in ['Shalem Investment', 'Water Unite Impact', 'Sukoon Water']
                 if name in visible]
    citations = list(dict.fromkeys(
        url for label, url in source_links.items()
        if re.fullmatch(r'.+ et al\., \d{4}', label) and label in visible
    ))
    publisher = BASE + '/#organization'
    author = BASE + '/#steve-hedden'
    image = BASE + '/figures/stunting-evidence-map.svg'
    graph = [
        {'@type': 'Organization', '@id': publisher, 'name': 'Universal Evidence',
         'url': BASE + '/', 'logo': {'@type': 'ImageObject',
         'url': BASE + '/assets/ue-mark-transparent.svg'}},
        {'@type': 'Person', '@id': author, 'name': 'Steve Hedden'},
        {'@type': 'WebSite', '@id': BASE + '/#website', 'url': BASE + '/',
         'name': 'Universal Evidence', 'publisher': {'@id': publisher}, 'inLanguage': 'en'},
        {'@type': 'WebPage', '@id': URL + '#webpage', 'url': URL, 'name': title,
         'description': DESCRIPTION, 'isPartOf': {'@id': BASE + '/#website'},
         'mainEntity': {'@id': URL + '#article'}, 'inLanguage': 'en'},
        {'@type': 'BlogPosting', '@id': URL + '#article', 'url': URL,
         'mainEntityOfPage': {'@id': URL + '#webpage'}, 'headline': title,
         'description': DESCRIPTION, 'author': {'@id': author},
         'publisher': {'@id': publisher}, 'datePublished': PUBLISHED,
         'dateModified': MODIFIED, 'inLanguage': 'en', 'isAccessibleForFree': True,
         'articleSection': 'Journal',
         'image': {'@type': 'ImageObject', 'url': image, 'width': 1440, 'height': 1420,
                   'caption': 'Childhood stunting: selected evidence and organizations in related work'},
         'about': [{'@type': 'DefinedTerm', '@id': url, 'url': url, 'name': name}
                   for url, name in topics.items()],
         'mentions': mentions, 'citation': citations},
    ]
    document = re.sub(r'<meta name="description" content="[^"]*">',
                      '<meta name="description" content="' + html.escape(DESCRIPTION, quote=True) + '">', document)
    document = re.sub(r'<meta property="og:description" content="[^"]*">',
                      '<meta property="og:description" content="' + html.escape(DESCRIPTION, quote=True) + '">', document)
    document = document.replace('property="og:type" content="website"',
                                'property="og:type" content="article"')
    extra = '<meta name="author" content="Steve Hedden">'
    extra += '<meta name="robots" content="index,follow,max-image-preview:large">'
    extra += '<meta name="twitter:description" content="' + html.escape(DESCRIPTION, quote=True) + '">'
    extra += '<meta property="article:published_time" content="' + PUBLISHED + '">'
    extra += '<meta property="article:modified_time" content="' + MODIFIED + '">'
    extra += '<script type="application/ld+json">' + json.dumps(
        {'@context': 'https://schema.org', '@graph': graph}, ensure_ascii=False,
        separators=(',', ':')).replace('<', '\\u003c') + '</script>'
    return document.replace('</head>', extra + '</head>')
