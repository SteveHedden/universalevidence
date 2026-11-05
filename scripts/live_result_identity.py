"""Reviewed identity lookup for transient source results, never a results catalogue.

Exact contextual keys protect version, scoring, population, recall and unit identity.
The source supplies every numerical value. Taxonomy supplies labels/States/notes.
"""
import re
from functools import lru_cache
from pathlib import Path
from rdflib import Graph, Namespace, RDF, URIRef
from rdflib.namespace import SKOS
from ctgov_indicator_context import indicator_key

ROOT = Path(__file__).resolve().parents[1]
UE = Namespace('https://universalevidence.com/ontology/')


def signature(paths):
    return tuple((str(p), p.stat().st_mtime_ns, p.stat().st_size) for p in paths)


@lru_cache(maxsize=2)
def _load(signature):
    crosswalk = Graph().parse(signature[0][0])
    taxonomy = Graph().parse(signature[1][0])
    mappings = {}
    for entry, _, raw in crosswalk.triples((None, UE.rawText, None)):
        targets = set(crosswalk.objects(entry, SKOS.exactMatch)) | set(crosswalk.objects(entry, SKOS.closeMatch))
        mappings.setdefault(str(raw), set()).update(targets)
    definitions = {}
    for target in taxonomy.subjects(RDF.type, UE.Indicator):
        labels = list(taxonomy.objects(target, SKOS.prefLabel))
        definitions[str(target)] = {
            'indicator': str(target), 'label': str(labels[0]) if labels else str(target).rsplit('/', 1)[-1],
            'states': sorted(str(s) for s in taxonomy.objects(target, UE.measures)),
            'definition': '\n'.join(str(s) for s in taxonomy.objects(target, SKOS.definition)),
            'scope_notes': [str(s) for s in taxonomy.objects(target, SKOS.scopeNote)],
        }
    return mappings, definitions


def identities():
    return _load(signature([ROOT/'vocabularies/crosswalks/ctgov-indicators-crosswalk.ttl',
                            ROOT/'vocabularies/indicators.ttl']))


def contextual_measure(study_id, outcome, class_index=None, category_index=None):
    mappings, definitions = identities()
    key = indicator_key(study_id, outcome, class_index, category_index)
    targets = mappings.get(key, set())
    if len(targets) != 1:
        return None
    definition = definitions.get(str(next(iter(targets))))
    if not definition or not definition['states']:
        return None
    unit = ' '.join(str(outcome.get('unitOfMeasure') or '').casefold().split())
    # These are quantity families, not aliases for an instrument or scoring algorithm.
    family = ({'units on a scale': 'score', 'score on a scale': 'score',
               'score': 'score', 'scores': 'score',
               'z-score': 'standardized_score', 'z score': 'standardized_score',
               'z-score length-for-age': 'standardized_score',
               'z scores': 'standardized_score', 'z-scores': 'standardized_score',
               'standard deviation': 'standardized_score', 'standard deviation score': 'standardized_score',
               'change in z score': 'standardized_score', 'change in score on a scale': 'score',
               'cm': 'length', 'centimeter (cm)': 'length', 'centimeters (cm)': 'length',
               'centimeter per year': 'length_rate', 'cm/year': 'length_rate',
               'points': 'score', 'participants': 'participant_count',
               'self-harm behaviors': 'event_frequency', 'events': 'event_frequency',
               'pg/mg': 'concentration',
               'percentage of participants': 'participant_percentage'}).get(unit)
    if family is None:
        return None
    statistic = outcome.get('paramType')
    # A source percentage is neither a participant count nor a transformed score.
    # Other percentage statistics need a separately reviewed contract.
    if family == 'participant_percentage' and statistic != 'NUMBER':
        return None
    if family == 'participant_count' and statistic != 'COUNT_OF_PARTICIPANTS':
        return None
    if family != 'participant_count' and statistic not in ('MEAN', 'LEAST_SQUARES_MEAN', 'MEDIAN', 'NUMBER'):
        return None
    # An exact contextual identity permits reading the source unit; it does not
    # license rescaling, a growth-reference substitution, or an inferred effect.
    title = ' '.join(str(outcome.get('title') or '').casefold().split())
    description = ' '.join(str(outcome.get('description') or '').casefold().split())
    explicit_change = (
        unit.startswith('change in ')
        or bool(re.match(r'^(?:part [a-z0-9]+:\s*)?(?:absolute )?change from baseline in ', title))
        or (title.startswith('change in ') and (description.startswith(
            ('evaluated by the change in ', 'change in ', 'change from baseline in ', 'these are differences from '))
            or 'change values calculated as:' in description
            or 'change from baseline was calculated as' in description
            or bool(re.search(r'\bchange in [^()]+\([^)]* - [^)]*\)', description))))
        or ('change from ' in description and ' to ' in description and unit.startswith('change '))
    )
    classes = outcome.get('classes', [])
    # A labelled assessment/subgroup requires its own cell interpretation; do
    # not spread an outcome-level change label across baseline/endline cells.
    unlabelled = (len(classes) == 1 and not classes[0].get('title')
                  and len(classes[0].get('categories', [])) == 1
                  and not classes[0]['categories'][0].get('title'))
    return {**definition, 'unit': outcome['unitOfMeasure'], 'family': family,
            'quantity': definition['label'], 'change': explicit_change and unlabelled, 'contextual': True,
            'identity_key': key}


def scoped_measure(study_id, outcome, ci, ki):
    mappings, _ = identities()
    parent = indicator_key(study_id, outcome)
    if parent in mappings:
        if has_scoped_identity(study_id, outcome):
            return None  # Broad outcome and scoped identities overlap; require review.
        return contextual_measure(study_id, outcome)
    class_key = indicator_key(study_id, outcome, ci)
    category_key = indicator_key(study_id, outcome, ci, ki)
    if class_key in mappings and category_key in mappings:
        return None  # Both apply to this cell, even if they currently share a target.
    if category_key in mappings:
        return contextual_measure(study_id, outcome, ci, ki)
    if class_key in mappings:
        return contextual_measure(study_id, outcome, ci)
    return None  # A different branch's mapping supplies no identity for this cell.


@lru_cache(maxsize=2)
def _interventions(sig):
    graph = Graph().parse(sig[0][0])
    result = {}
    for entry, _, raw in graph.triples((None, UE.rawText, None)):
        targets = set(graph.objects(entry, SKOS.exactMatch)) | set(graph.objects(entry, SKOS.closeMatch))
        result.setdefault(str(raw), set()).update(str(t) for t in targets)
    labels = preferred_labels({target for targets in result.values() for target in targets})
    return {k: [(t, labels.get(t, t.rsplit('/', 1)[-1])) for t in sorted(v)] for k, v in result.items()}


def reviewed_interventions():
    return _interventions(signature([ROOT/'vocabularies/crosswalks/ctgov-interventions-crosswalk.ttl']))


def has_scoped_identity(study_id, outcome):
    mappings, _ = identities()
    return any(indicator_key(study_id, outcome, i) in mappings or
               any(indicator_key(study_id, outcome, i, j) in mappings for j, _ in enumerate(c.get('categories', [])))
               for i, c in enumerate(outcome.get('classes', [])))


def intervention_discovery_terms(concept, mapping):
    """Source labels are retrieval hints only; attribution stays study-scoped."""
    from ctgov_intervention_identity import parse_key
    terms = set()
    for key, targets in mapping.items():
        parsed = parse_key(key)
        if parsed and len(targets) == 1 and targets[0][0] == concept:
            terms.add(parsed[1])
    return sorted(terms)


def scoped_intervention_match(study_id, names, concept, mapping):
    """Return matching and unmapped names; scoped conflicts cannot use aliases."""
    from ctgov_intervention_identity import contextual_key
    matched, unmapped = False, []
    for name in names:
        targets = mapping.get(contextual_key(study_id, name))
        if targets is None:
            unmapped.append(name)
        elif len(targets) == 1 and targets[0][0] == concept:
            matched = True
    return matched, unmapped


@lru_cache(maxsize=2)
def _preferred_labels(sig):
    graph = Graph()
    for path, _, _ in sig:
        graph.parse(path)
    labels = {}
    for subject in set(graph.subjects(SKOS.prefLabel, None)):
        values = list(graph.objects(subject, SKOS.prefLabel))
        english = [v for v in values if v.language in (None, 'en')]
        chosen = english or values
        if len(set(map(str, chosen))) == 1:
            labels[str(subject)] = str(chosen[0])
    return labels


def preferred_labels(concepts):
    labels = _preferred_labels(signature([ROOT/'vocabularies/interventions.ttl',
                                          ROOT/'vocabularies/indicators.ttl']))
    return {c: labels[c] for c in concepts if c in labels}
