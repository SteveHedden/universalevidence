"""IATI source-entry lookup. Equivalence of terms never asserts project delivery.

Crosswalks are source -> UE; reverse lookup supplies bounded retrieval clauses.
Coded retrieval is a candidate filter: the returned nested entry is authoritative.
"""
from pathlib import Path
import hashlib
import re
from rdflib import Graph, Namespace, URIRef
from rdflib.namespace import RDF, SKOS, DCTERMS
from scripts import iati_actors as actors
from scripts.iati_live import ENDPOINT
from scripts.iati_live_rules import contains, quote

ROOT = Path(__file__).resolve().parents[1]
IQ = Namespace('https://universalevidence.com/source/iati/config#')
UE = actors.UE
SOURCE = URIRef('https://universalevidence.com/vocab/sources/iati')
AXES = {'state': UE.ConditionAxis, 'intervention': UE.InterventionAxis}
# Executable field contracts; an arbitrary metadata field must not enable querying.
FIELDS = {'title': ('title_narrative', None), 'description': ('description_narrative', None),
          'sector': ('sector_code', 'sector_vocabulary'), 'tag': ('tag_code', 'tag_vocabulary'),
          'default-aid-type': ('default_aid_type_code', 'default_aid_type_vocabulary')}


def one(graph, subject, predicate, default=None):
    values = list(graph.objects(subject, predicate))
    if not values and default is not None:
        return default
    if len(values) != 1:
        raise ValueError('Expected one source/crosswalk value: ' + str(predicate))
    return str(values[0])


class SearchCrosswalk:
    def __init__(self, sources=None, crosswalk=None, *, root=None):
        root = Path(root or ROOT).resolve()
        sources = Path(sources or root / 'vocabularies/sources.ttl')
        registry = Graph().parse(sources)
        service = URIRef(one(registry, SOURCE, URIRef('http://www.w3.org/ns/dcat#accessService')))
        if one(registry, service, URIRef('http://www.w3.org/ns/dcat#endpointURL')) != ENDPOINT:
            raise ValueError('Unsupported IATI transport endpoint')
        self.rules = {}
        self.unsupported = set()
        self.candidate_review = False
        self.forward = {}
        versions = {str(sources): sources.read_bytes()}
        graphs = {}

        def local(relative):
            path = (root / relative).resolve()
            if not path.is_relative_to(root / 'vocabularies'):
                raise ValueError('Invalid vocabulary path')
            return path

        def graph_at(path):
            if path not in graphs:
                versions[str(path)] = path.read_bytes()
                graphs[path] = Graph().parse(path)
            return graphs[path]

        def add(axis, concept, term):
            actors.concept(concept, axis)
            rule = self.rules.setdefault((axis, concept), {'concept': concept, 'terms': []})
            if term not in rule['terms']:
                rule['terms'].append(term)
            if len(rule['terms']) > 60:
                raise ValueError('Too many bounded source terms')

        for node in sorted(registry.objects(SOURCE, UE.fieldMapping), key=str):
            source = one(registry, node, UE.sourceField)
            kind = one(registry, node, IQ.valueKind)
            axes = [axis for axis, uri in AXES.items() if uri in registry.objects(node, UE.feedsAxis)]
            enabled = one(registry, node, IQ.enabled, 'false') == 'true'
            if kind == 'narrative':
                if not enabled:
                    continue
                field = one(registry, node, IQ.queryField)
                if source not in ('title', 'description') or FIELDS[source][0] != field:
                    raise ValueError('Unsupported narrative binding')
                path = Path(crosswalk) if crosswalk else local(one(registry, node, UE.crosswalk))
                graph = graph_at(path)
                seen = set()
                for entry in graph.subjects(RDF.type, UE.CrosswalkEntry):
                    raw = one(graph, entry, UE.rawText)
                    if raw in seen:
                        raise ValueError('Duplicate rawText identity')
                    seen.add(raw)
                    if one(graph, entry, IQ.reviewStatus, 'candidate') != 'approved':
                        continue
                    if not raw.strip() or len(raw) > 120:
                        raise ValueError('Invalid bounded phrase')
                    for predicate in (DCTERMS.creator, DCTERMS.date, DCTERMS.description, DCTERMS.source):
                        one(graph, entry, predicate)
                    if list(graph.objects(entry, SKOS.broadMatch)):
                        raise ValueError('Broad mappings are not executable')
                    matches = [(p, c) for p in (SKOS.exactMatch, SKOS.closeMatch) for c in graph.objects(entry, p)]
                    if not matches:
                        raise ValueError('Approved entry lacks exact/close mapping')
                    for predicate, concept in matches:
                        axis = next((a for a in axes if str(concept).startswith(
                            'https://universalevidence.com/vocab/' + ('states/' if a == 'state' else 'interventions/'))), None)
                        if axis is None:
                            raise ValueError('Crosswalk target does not support binding axis')
                        target_graph = graph_at(local(one(registry, AXES[axis], UE.targetFile)))
                        if (concept, RDF.type, UE.State if axis == 'state' else UE.Intervention) not in target_graph:
                            raise ValueError('Crosswalk target is not a canonical axis concept')
                        term = {'kind': 'mention', 'entry': str(entry), 'phrase': raw,
                                'binding': str(node), 'source': source, 'field': field, 'match': str(predicate)}
                        add(axis, str(concept), term)
                        self.forward.setdefault(raw, set()).add(str(concept))
            elif kind == 'code':
                # Existing thesaurus links are not automatically approved for IATI execution.
                reviewed = set(registry.objects(node, DCTERMS.references))
                if not reviewed:
                    if enabled:
                        raise ValueError('Enabled code binding lacks reviewed external identities')
                    continue
                review_subjects = set(registry.objects(node, DCTERMS.subject))
                if len(reviewed) != 1 or len(review_subjects) != 1:
                    raise ValueError('Code review must scope one concept/link pair per binding')
                for axis in axes:
                    taxonomy = graph_at(local(one(registry, AXES[axis], UE.targetFile)))
                    for predicate in (SKOS.exactMatch, SKOS.closeMatch):
                        for concept, external in taxonomy.subject_objects(predicate):
                            if external not in reviewed or concept not in review_subjects:
                                continue
                            if (concept, RDF.type, UE.State if axis == 'state' else UE.Intervention) not in taxonomy:
                                raise ValueError('Code target is not a canonical axis concept')
                            if not enabled:
                                self.unsupported.add((axis, str(concept)))
                                continue
                            if one(registry, node, IQ.reviewStatus, 'candidate') != 'approved':
                                raise ValueError('Unreviewed code binding')
                            field = one(registry, node, IQ.queryField)
                            vocab_field = one(registry, node, IQ.vocabularyField)
                            if source not in FIELDS or FIELDS[source] != (field, vocab_field):
                                raise ValueError('Unsupported coded binding')
                            prefix = one(registry, node, IQ.conceptPrefix)
                            if not str(external).startswith(prefix):
                                raise ValueError('External concept has wrong vocabulary identity')
                            code = str(external)[len(prefix):]
                            if not re.fullmatch(r'[A-Za-z0-9_.-]{1,80}', code):
                                raise ValueError('Invalid scoped vocabulary code')
                            vocabulary = one(registry, node, IQ.vocabularyCode)
                            publisher = one(registry, node, IQ.publisherRef, '')
                            vocabulary_uri = one(registry, node, IQ.vocabularyURI, '')
                            version = one(registry, node, IQ.vocabularyVersion, '')
                            if vocabulary in ('98', '99') and not (publisher and vocabulary_uri):
                                raise ValueError('Publisher codes require publisher and vocabulary URI')
                            term = {'kind': 'code', 'entry': str(external), 'binding': str(node),
                                    'source': source, 'field': field, 'vocabulary_field': vocab_field,
                                    'code': code, 'vocabulary': vocabulary,
                                    'default': one(registry, node, IQ.defaultVocabulary, ''),
                                    'vocabulary_uri': vocabulary_uri, 'publisher': publisher,
                                    'version': version, 'match': str(predicate)}
                            add(axis, str(concept), term)
            else:
                raise ValueError('Unknown source value kind')
        self.version = hashlib.sha256(b'iati-source-entries-2\0' + b'\0'.join(
            versions[k] for k in sorted(versions))).hexdigest()

    def get(self, axis, uri):
        return self.rules.get((axis, uri)) if uri else None

    def query(self, rule):
        clauses = []
        for term in rule['terms']:
            if term['kind'] == 'mention':
                clauses.append(term['field'] + ':' + quote(term['phrase']))
            else:
                # Flat arrays only narrow candidates. Missing default vocabulary remains retrievable.
                vocab = term['vocabulary_field'] + ':' + quote(term['vocabulary'])
                if term['default'] == term['vocabulary']:
                    vocab = '(' + vocab + ' OR (*:* AND -' + term['vocabulary_field'] + ':[* TO *]))'
                clauses.append('(' + term['field'] + ':' + quote(term['code']) + ' AND ' + vocab + ')')
        return '(' + ' OR '.join(sorted(set(clauses))) + ')'

    def support(self, raw, rule):
        found = []
        publishers = {v.get('@ref') for v in raw.get('reporting-org', []) if isinstance(v, dict)}
        for term in rule['terms']:
            entries = raw.get(term['source'], [])
            if not isinstance(entries, list):
                continue
            for i, entry in enumerate(entries):
                if not isinstance(entry, dict):
                    continue
                common = {'kind': term['kind'], 'concept': rule['concept'], 'crosswalk': term['entry'],
                          'binding': term['binding'], 'term_match': term['match']}
                if term['kind'] == 'code':
                    if (entry.get('@code') != term['code'] or
                        entry.get('@vocabulary', term['default']) != term['vocabulary'] or
                        term['publisher'] and term['publisher'] not in publishers or
                        term['vocabulary_uri'] and entry.get('@vocabulary-uri') != term['vocabulary_uri'] or
                        term['version'] and entry.get('@vocabulary-version') != term['version']):
                        continue
                    found.append({**common, 'path': [term['source'], i], 'code': term['code'],
                                  'vocabulary': term['vocabulary'], 'vocabulary_uri': term['vocabulary_uri'],
                                  'publisher': term['publisher'], 'version': term['version'],
                                  'text': f"Source code {term['code']} (vocabulary {term['vocabulary']})"})
                    continue
                narratives = entry.get('narrative', [])
                if not isinstance(narratives, list):
                    continue
                for j, narrative in enumerate(narratives):
                    text = narrative.get('text()', '') if isinstance(narrative, dict) else ''
                    phrase = term['phrase']
                    if not isinstance(text, str) or not contains(text, phrase):
                        continue
                    hit = re.search(re.escape(phrase), text, re.I)
                    if not hit:
                        continue
                    left, right = max(0, hit.start()-160), min(len(text), hit.end()+220)
                    found.append({**common, 'path': [term['source'], i, 'narrative', j, 'text()'],
                                  'phrase': phrase, 'text': text[left:right], 'excerpt_start': left, 'excerpt_end': right})
        return found[:12]
