"""Additive, source-neutral comparison projection over existing UE/CIDS results.

Component identities are reviewed TTL assets, matched to exact source context.
No network, writes, AI, numerical inference, or ontology modifications at runtime.
"""
import hashlib
import json
from rdflib import Graph, Namespace, URIRef, Literal, RDF, RDFS
from rdflib.namespace import DCTERMS

UE = Namespace('https://universalevidence.com/ontology/')
PICO = Namespace('http://data.cochrane.org/ontologies/pico/')


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':')).encode()).hexdigest()


def source_groups(row):
    comp = row.get('comparison') or {}
    if comp.get('kind') == 'between-groups' and comp.get('intervention') and comp.get('reference'):
        return [dict(title=comp['intervention'], components=comp.get('intervention_concepts')),
                dict(title=comp['reference'], components=comp.get('comparator_concepts'))], True
    context = comp.get('context') or {}
    if context.get('group'):
        return [context['group']], False
    groups = context.get('groups') or []
    # Source groupIds order alone is not an estimate orientation.
    operands = comp.get('operands') or []
    oriented = (comp.get('kind') == 'derived_comparison' and
                comp.get('formula') == 'left - right' and
                comp.get('orientation') == 'forward' and len(groups) == 2 and
                len(operands) == 2 and
                [g.get('id') for g in groups] == [o.get('groupId') for o in operands])
    return groups, oriented


def load_components(path):
    graph = Graph().parse(path)
    index = {}
    for group in graph.subjects(RDF.type, PICO.InterventionGroup):
        study = graph.value(group, DCTERMS.isPartOf)
        key = graph.value(group, DCTERMS.identifier)
        if study is None or key is None:
            raise ValueError('Component mapping requires study and source-context fingerprint')
        components = sorted({str(c) for descriptor in graph.objects(group, PICO.intervention)
                             for c in graph.objects(descriptor, UE.taxonomyConcept)})
        record = {'uri': str(group), 'components': components,
                  'complete': Literal('reviewed-complete-component-set') in graph.objects(group, DCTERMS.type)}
        identity = (str(study), str(key))
        if identity in index:
            raise ValueError('Ambiguous component mapping')
        index[identity] = record
    return graph, index


def project(rows, graph, mappings, selected_interventions=()):
    """Enrich rows and transient RDF; existing measurements remain untouched."""
    mapping_graph, index = mappings
    selected = set(map(str, selected_interventions))
    graph.bind('pico', PICO)
    for row in rows:
        raw_groups, oriented = source_groups(row)
        groups = []
        for raw in raw_groups:
            match = index.get((row['study'], fingerprint(raw)))
            group = dict(match) if match else {
                'uri': row['study'] + '/result-group/' + fingerprint(raw),
                'components': [], 'complete': False}
            group['label'] = raw.get('title', '')
            groups.append(group)
            node = URIRef(group['uri'])
            graph.add((node, RDF.type, PICO.InterventionGroup))
            graph.add((node, RDFS.label, Literal(group['label'])))
            graph.add((node, DCTERMS.isPartOf, URIRef(row['study'])))
            if match:
                for triple in mapping_graph.triples((node, None, None)):
                    graph.add(triple)
                for descriptor in mapping_graph.objects(node, PICO.intervention):
                    for triple in mapping_graph.triples((descriptor, None, None)):
                        graph.add(triple)
        model = {'groups': groups, 'orientation_known': oriented,
                 'selected_intervention_role': 'unknown', 'confidence': 'not assessed'}
        measurement = URIRef(row['measurement'])
        if oriented and len(groups) == 2:
            # Group identity is independent of indicator, estimate, model and time.
            pair = row['study'] + '/comparison/' + fingerprint([g['uri'] for g in groups])
            context = URIRef(pair)
            graph.add((context, RDF.type, PICO.PICO))
            graph.add((context, DCTERMS.isPartOf, URIRef(row['study'])))
            graph.add((URIRef(row['study']), UE.studyContext, context))
            for outcome_group in graph.objects(measurement, UE.specificationGroup):
                graph.add((context, PICO.outcomeGroup, outcome_group))
            for role, group in zip((UE.comparisonIntervention, UE.comparisonComparator), groups):
                for component in group['components']:
                    graph.add((measurement, role, URIRef(component)))
            graph.add((measurement, DCTERMS.isPartOf, context))
            graph.add((context, PICO.interventionGroup, URIRef(groups[0]['uri'])))
            graph.add((context, PICO.comparatorGroup, URIRef(groups[1]['uri'])))
            model['comparison'] = pair
            if all(g['complete'] for g in groups):
                left, right = (set(g['components']) for g in groups)
                model.update(shared_components=sorted(left & right),
                             intervention_only=sorted(left-right), comparator_only=sorted(right-left))
                if selected:
                    l, r = left & selected, right & selected
                    if l and r:
                        role = 'present in both groups; effect not isolated'
                    elif not l and not r:
                        role = 'absent from both groups'
                    elif len(left ^ right) == 1:
                        role = 'only mapped component difference'
                    else:
                        role = 'part of a differing bundle; effect not isolated'
                    model['selected_intervention_role'] = role
        else:
            for group in groups:
                graph.add((measurement, DCTERMS.references, URIRef(group['uri'])))
        # Do not coerce a Subject (e.g. river) into a human PICO Population.
        indicator = graph.value(measurement, UE.ofIndicator)
        state = graph.value(measurement, UE.ofState)
        model['state'] = str(state) if state else None
        model['indicator'] = str(indicator) if indicator else row.get('indicator')
        model['subjects'] = sorted({str(s) for bearer in (indicator, state) if bearer
                                    for s in graph.objects(bearer, UE.bearer)})
        row['result_model'] = model
    return rows
