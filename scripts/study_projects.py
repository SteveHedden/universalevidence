"""Reviewed project metadata; independent of live research discovery/results.

Publication uses Teacher's append-only reviewed snapshot protocol. A complete
snapshot is authoritative; the API only consumes active, hash-verified snapshots.
"""
import argparse
from collections import Counter
from datetime import datetime
from functools import lru_cache
import json
import os
import re
from pathlib import Path
from urllib.parse import urlparse

from scripts import reviewed_snapshot as tx

STORE = 'evidence/study-implementation'
AGENTS = 'evidence/agents'
STATE = 'https://universalevidence.com/vocab/states/'
INTERVENTION = 'https://universalevidence.com/vocab/interventions/'


def iri(value):
    parsed = urlparse(value)
    if parsed.scheme not in ('http', 'https') or not parsed.netloc or any(c.isspace() for c in value):
        raise ValueError('Invalid absolute identity: ' + value)
    return value


def date(value):
    if not value:
        raise ValueError('Review date required')
    if datetime.fromisoformat(value.replace('Z', '+00:00')).tzinfo is None:
        raise ValueError('Review timestamp requires timezone')


def refs(value):
    """Find all sealed references, retaining explicit source locators separately."""
    if isinstance(value, dict):
        if 'path' in value and 'sha256' in value:
            yield {'path': value['path'], 'sha256': value['sha256']}
        for v in value.values():
            yield from refs(v)
    elif isinstance(value, list):
        for v in value:
            yield from refs(v)


def support(items):
    if not items:
        raise ValueError('Located source evidence required')
    for item in items:
        if not all(item.get(k) for k in ('path', 'sha256', 'locator', 'url')):
            raise ValueError('Evidence requires capture, hash, locator and source URL')
        iri(item['url'])


def identity_index(agents):
    result = {}
    for org in agents.get('organizations', []):
        for role in org.get('roles', []):
            result[role['uri']] = {'id': org['id'], 'name': org['name'], 'study_uri': role['study_uri']}
    return result


def validate(data, cohort, agents, previous=None):
    if data.get('version') != 1:
        raise ValueError('Unsupported project snapshot version')
    date(data.get('reviewed_at'))
    expected = {s['id'] for s in cohort['slices']}
    assignments = data['assignments']
    if len(assignments) != len(expected) or {a['id'] for a in assignments} != expected:
        raise ValueError('Every frozen assignment needs exactly one disposition')
    for a in assignments:
        if a.get('status') not in ('eligible', 'excluded', 'blocked', 'unresolved', 'duplicate') or not a.get('reason') or not a.get('evidence'):
            raise ValueError('Assignment needs an evidenced disposition')
    identities = identity_index(agents)
    all_occurrences = {r['uri']: r for category in ('organizations', 'unresolved', 'sites') for entry in agents.get(category, []) for r in entry.get('roles', [])}
    all_occurrences.update({r['uri']: r for r in agents.get('sites', []) if 'uri' in r})
    seen, aliases, context_ids, component_owners = set(), {}, set(), {}
    for study in data['studies']:
        iri(study['id']); iri(study['source_url']); date(study['reviewed_at'])
        patterns = {'ctgov': r'NCT[0-9]{8}', 'aea': r'AEARCTR-[0-9]{7}', 'isrctn': r'ISRCTN[0-9]{8}'}
        if study['source'] in patterns and not re.fullmatch(patterns[study['source']], study['study_id']):
            raise ValueError('Malformed registry study identifier')
        if not study['id'].startswith('https://universalevidence.com/studies/'):
            raise ValueError('Use reviewed UE study identity')
        if study['id'] in seen or not study.get('title'):
            raise ValueError('Duplicate/malformed study')
        seen.add(study['id'])
        if study.get('status') in ('current', 'operating', 'verified-current'):
            raise ValueError('Retained study metadata cannot assert current operation')
        for a in [{'source': study['source'], 'study_id': study['study_id']}, *study.get('aliases', [])]:
            if a['source'] in patterns and not re.fullmatch(patterns[a['source']], a['study_id']):
                raise ValueError('Malformed registry alias')
            if (a['source'], a['study_id']) != (study['source'], study['study_id']) and not a.get('verification'):
                raise ValueError('Cross-registry alias needs independent verification')
            key = (a['source'], a['study_id'])
            if key in aliases and aliases[key] != study['id']:
                raise ValueError('Registry alias belongs to multiple study cards')
            aliases[key] = study['id']
        components, contexts = set(), set()
        for c in study['contexts']:
            iri(c['id'])
            if c['id'] in context_ids:
                raise ValueError('Duplicate context identity')
            context_ids.add(c['id']); contexts.add(c['id'])
            if c.get('status', 'active') not in ('active', 'withdrawn', 'superseded'):
                raise ValueError('Invalid attribution revision status')
            if not c['state'].startswith(STATE) or not c.get('reason'):
                raise ValueError('Reviewed State intent rationale required')
            support(c['intent_evidence'])
            if not c['interventions']:
                raise ValueError('Context requires intervention attribution')
            for intervention in c['interventions']:
                if not intervention['uri'].startswith(INTERVENTION):
                    raise ValueError('Canonical intervention required')
                iri(intervention['component_id']); components.add(intervention['component_id'])
                owner = component_owners.setdefault(intervention['component_id'], c['id'])
                if owner != c['id']:
                    raise ValueError('Component scope reused across contexts')
                if intervention.get('arm', 'intervention') not in ('intervention', 'comparator'):
                    raise ValueError('Explicit arm required')
        for r in study['roles']:
            iri(r['occurrence']); support(r['evidence'])
            if not r.get('name') or not r.get('role'):
                raise ValueError('Reported name and role required')
            scope = r['scope']
            valid = {'study': {study['id']}, 'context': contexts, 'component': components}
            if scope.get('id') not in valid.get(scope.get('kind'), set()):
                raise ValueError('Role outside supported study/context/component scope')
            if r['role'] == 'implementer' and (scope['kind'] == 'study' or not r.get('delivery_rationale')):
                raise ValueError('Implementation requires explicit scoped delivery evidence')
            occurrence = all_occurrences.get(r['occurrence'])
            valid_studies = {study['id'], *(f"https://universalevidence.com/studies/{a['source']}/{a['study_id']}" for a in study.get('aliases', []))}
            if not occurrence or occurrence['study_uri'] not in valid_studies:
                raise ValueError('Source occurrence does not belong to this verified study')
            if r.get('organization_id'):
                resolved = identities.get(r['occurrence'])
                if not resolved or resolved['id'] != r['organization_id']:
                    raise ValueError('Organization identity must match active reviewed occurrence')
    # Corrections retain old studies and contexts with explicit withdrawal/supersession.
    if previous:
        old_by_id = {s['id']: s for s in previous['studies']}
        for study in data['studies']:
            old = old_by_id.get(study['id'])
            if old and study != old and study.get('revision_of') != tx.sha(tx.encoded(old)):
                raise ValueError('Changed study requires prior reviewed payload hash')
        if {s['id'] for s in previous['studies']} - seen:
            raise ValueError('Retain withdrawn study identities in revisions')
        old_contexts = {c['id'] for s in previous['studies'] for c in s['contexts']}
        if old_contexts - context_ids:
            raise ValueError('Retain withdrawn/superseded contexts in revisions')


def graph_bytes(data):
    """Standard terms and existing UE study/PICO profile; no outcome assertions."""
    from rdflib import Graph, URIRef as U, Literal as L, Namespace
    from rdflib.namespace import RDF, DCTERMS, XSD
    g = Graph()
    UE = Namespace('https://universalevidence.com/ontology/')
    PICO = Namespace('http://data.cochrane.org/ontologies/pico/')
    PROV = Namespace('http://www.w3.org/ns/prov#')
    SCHEMA = Namespace('https://schema.org/')
    dataset = U('https://universalevidence.com/study-implementation/snapshot')
    g.add((dataset, RDF.type, PROV.Entity))
    g.add((dataset, DCTERMS.description, L(json.dumps(data, ensure_ascii=False, sort_keys=True))))
    def evidence(subject, items):
        for e in items:
            n = U('https://universalevidence.com/study-implementation/evidence/' + tx.sha(tx.encoded(e)))
            g.add((subject, PROV.wasDerivedFrom, n)); g.add((n, RDF.type, PROV.Entity))
            g.add((n, DCTERMS.source, U(e['url']))); g.add((n, DCTERMS.identifier, L(e['sha256'])))
            g.add((n, DCTERMS.description, L(json.dumps(e, sort_keys=True))))
    for s in data['studies']:
        study = U(s['id']); g.add((study, RDF.type, UE.Study)); g.add((study, DCTERMS.title, L(s['title'])))
        g.add((study, DCTERMS.source, U(s['source_url'])))
        for c in s['contexts']:
            context = U(c['id']); g.add((study, UE.studyContext, context)); g.add((context, RDF.type, PICO.PICO))
            g.add((context, SCHEMA.creativeWorkStatus, L(c.get('status', 'active'))))
            g.add((context, DCTERMS.description, L(c['reason'])))
            g.add((context, PROV.generatedAtTime, L(s['reviewed_at'], datatype=XSD.dateTime)))
            g.add((context, UE.intendedState, U(c['state'])))
            evidence(context, c['intent_evidence'])
            for i in c['interventions']:
                component = U(i['component_id']); group = U(i['component_id'] + '/group')
                g.add((context, PICO.comparatorGroup if i.get('arm') == 'comparator' else PICO.interventionGroup, group))
                g.add((group, RDF.type, PICO.InterventionGroup)); g.add((group, PICO.intervention, component))
                g.add((component, RDF.type, PICO.Intervention)); g.add((component, UE.taxonomyConcept, U(i['uri'])))
                g.add((component, DCTERMS.description, L(i['label'])))
        for r in s['roles']:
            role = U('https://universalevidence.com/study-implementation/roles/' + tx.sha(tx.encoded(r)))
            g.add((role, RDF.type, SCHEMA.Role)); g.add((role, SCHEMA.about, U(r['scope']['id'])))
            g.add((role, SCHEMA.member, U(r['occurrence']))); g.add((role, SCHEMA.roleName, L(r['role'])))
            g.add((role, DCTERMS.description, L(json.dumps(r, sort_keys=True))))
            evidence(role, r['evidence'])
    return ''.join(sorted(g.serialize(format='nt').splitlines(keepends=True))).encode()


def bind_followups(data):
    """Fingerprint proposed facts as well as source bytes for idempotent follow-ups."""
    for assignment in data['assignments']:
        key = (assignment['source'], assignment['study_id'])
        studies = [s for s in data['studies'] if key in {(s['source'], s['study_id']),
                   *((a['source'], a['study_id']) for a in s.get('aliases', []))}]
        semantic = [{k: v for k, v in study.items() if k != 'reviewed_at'} for study in studies]
        assignment['proposed_facts_sha256'] = tx.sha(tx.encoded(semantic))
        assignment['identity_snapshot'] = data['agents_transaction']


def prepare(root, input_path, out, proposer):
    root, out = Path(root), tx.inside(root, out)
    data = json.loads(tx.inside(root, input_path).read_bytes())
    cohort = json.loads(tx.checked(root, data['cohort']))
    agent_receipt = tx.read_head(root, AGENTS)
    if not agent_receipt or agent_receipt['transaction'] != data['agents_transaction']:
        raise ValueError('Stale Agents snapshot')
    agents = json.loads(tx.checked(root, agent_receipt['outputs']['agents.json']))
    prev = tx.read_head(root, STORE)
    previous = json.loads(tx.checked(root, prev['outputs']['projects.json'])) if prev else None
    validate(data, cohort, agents, previous)
    inventory = {(r['source'], r['study_id']): r for r in agents['source_inventory']}
    for s in data['studies']:
        keys = {(s['source'], s['study_id']), *((a['source'], a['study_id']) for a in s.get('aliases', []))}
        s['source_records'] = [inventory[k] for k in sorted(keys) if k in inventory]
        if not s['source_records']:
            raise ValueError('Study must belong to reviewed frozen source inventory')
    bind_followups(data)
    all_refs = list(refs(data)) + [tx.reference(root, input_path), agent_receipt['outputs']['agents.json'],
                                 tx.reference(root, __file__)]
    # Seal the model contract and its independent review before population publication.
    contract = data['contract']; model_review = data['model_review']
    review = json.loads(tx.checked(root, model_review))
    if not review.get('approved') or review.get('contract_sha256') != contract['sha256'] or review.get('reviewer') == proposer:
        raise ValueError('Independent exact model contract approval required')
    for ref in all_refs: tx.checked(root, ref)
    out.mkdir(parents=True, exist_ok=True)
    rdf = graph_bytes(data)
    from pyshacl import validate as shacl_validate
    conforms, _, report = shacl_validate(rdf.decode(), data_graph_format='nt', shacl_graph=str(root / 'ontology/study-implementation-shapes.ttl'))
    if not conforms: raise ValueError(report)
    all_refs.extend(tx.reference(root, p) for p in ('ontology/study-implementation-shapes.ttl', 'ontology/study-implementation.ttl'))
    tx.immutable(out / 'projects.json', tx.encoded(data)); tx.immutable(out / 'projects.ttl', rdf)
    proposal = {'proposer': proposer, 'parent': prev['transaction'] if prev else None,
                'evidence': sorted({(r['path'], r['sha256']): r for r in all_refs}.values(), key=lambda r: r['path']),
                'outputs': {n: tx.reference(root, out / n) for n in ('projects.json', 'projects.ttl')}}
    tx.immutable(out / 'proposal.json', tx.encoded(proposal))
    return proposal


def active(root):
    p, a = tx.read_head(root, STORE), tx.read_head(root, AGENTS)
    if not p or not a:
        return None
    data = json.loads(tx.checked(root, p['outputs']['projects.json']))
    if graph_bytes(data) != tx.checked(root, p['outputs']['projects.ttl']):
        raise ValueError('Reviewed JSON/RDF disagreement')
    return (data, json.loads(tx.checked(root, a['outputs']['agents.json'])), p, a)


@lru_cache(maxsize=4)
def cached(root, project_head, agent_head):
    # Both heads are in the key. A publication or identity correction invalidates it.
    result = active(Path(root))
    if result and (result[2]['transaction'] != json.loads(project_head)['transaction'] or
                   result[3]['transaction'] != json.loads(agent_head)['transaction']):
        raise ValueError('Snapshot changed during read; retry')
    return result


def project_cards(data, agents, state, intervention):
    resolved = identity_index(agents)
    inventory = {(r['source'], r['study_id']): r for r in agents['source_inventory']}
    projects, stale = [], []
    for s in data['studies']:
        if s.get('revision_status', 'active') != 'active':
            continue
        if any(inventory.get((r['source'], r['study_id']), {}).get('record') != r.get('record')
               for r in s.get('source_records', [])):
            stale.append(s['id']); continue
        contexts = [c for c in s['contexts'] if c.get('status', 'active') == 'active' and c['state'] == state
                    and any(i['uri'] == intervention and i.get('arm', 'intervention') == 'intervention' for i in c['interventions'])]
        if not contexts: continue
        context_ids = {c['id'] for c in contexts}
        components = {i['component_id'] for c in contexts for i in c['interventions'] if i['uri'] == intervention and i.get('arm', 'intervention') == 'intervention'}
        roles = []
        for r in s['roles']:
            scope = r['scope']
            if not (scope['kind'] == 'study' or scope['id'] in context_ids or scope['id'] in components):
                continue
            identity = resolved.get(r['occurrence'])
            roles.append({'name': identity['name'] if identity else r['name'], 'organization_id': identity['id'] if identity else None,
                          'identity_status': 'verified' if identity else 'unresolved', 'role': r['role'],
                          'scope': scope, 'evidence': [{k: e[k] for k in ('url', 'locator', 'quote') if k in e} for e in r['evidence']]})
        implementers = [r for r in roles if r['role'] == 'implementer' and r['identity_status'] == 'verified']
        projects.append({'id': s['id'], 'title': s['title'], 'source': s['source'], 'study_id': s['study_id'],
                         'aliases': s.get('aliases', []), 'source_url': s['source_url'], 'source_date': s.get('source_date'),
                         'reviewed_at': s['reviewed_at'], 'geography': s.get('geography', []), 'dates': s.get('dates', []),
                         'status': s['status'], 'provenance': 'reviewed-study',
                         'match_reasons': [c['reason'] for c in contexts],
                         'contexts': [{'id': c['id'], 'components': c['interventions']} for c in contexts],
                         'implementers': implementers, 'other_roles': [r for r in roles if r not in implementers],
                         'gaps': s.get('gaps', []),
                         'missing_role': None if implementers else 'Implementing organization not yet verified'})
    projects.sort(key=lambda s: (s['title'].casefold(), s['id']))
    return projects, stale


def lookup(state='', intervention='', context='', root=None):
    result = {'status': 'missing-context', 'projects': [], 'count': 0, 'errors': [],
              'scope': 'Frozen reviewed Stunting cohort; both State and Intervention required. Other study filters are not applied.',
              'freshness': 'Retained reviewed metadata; current operation is not freshly verified.'}
    if not state or not intervention: return result
    try:
        values = json.loads(context or '{}').get('values', {})
        if len(values.get('state', [])) > 1:
            result['status'] = 'unsupported-context'; return result
    except (ValueError, AttributeError, TypeError):
        result['status'] = 'invalid-context'; return result
    root = Path(root or os.getenv('UE_STUDY_PROJECT_ROOT') or Path(__file__).resolve().parents[1]).resolve()
    try:
        heads = [(root / store / 'head.json').read_bytes() for store in (STORE, AGENTS)]
        snapshot = cached(str(root), *heads)
        if not snapshot:
            result['status'] = 'unavailable'; return result
        data, agents, receipt, agent_receipt = snapshot
        cards, stale = project_cards(data, agents, state, intervention)
        from scripts.organization_followups import merge_cards, cards as followup_cards
        cards = merge_cards(root, cards, state, intervention)
        related = [p for p in followup_cards(root, state, intervention, include_broader=True)
                   if p['relevance_bases'] == ['broader-nutrition']]
        approval = json.loads(tx.checked(root, receipt['approval']))
        reviewed_at = approval['reviewed_at']
        date(reviewed_at)
        for card in cards:
            if card.get('provenance') != 'reviewed-organization-followup': card['reviewed_at'] = reviewed_at
        result.update(status='partial' if stale else 'ok' if cards else 'empty', projects=cards, count=len(cards),
                      related_projects=related, related_count=len(related),
                      version=receipt['transaction'], agents_version=agent_receipt['transaction'],
                      reviewed_at=reviewed_at, cohort_sha256=data['cohort']['sha256'],
                      stale_source_count=len(stale), errors=['source-revision-needs-review'] if stale else [])
    except (OSError, ValueError, KeyError, TypeError):
        result.update(status='unavailable', errors=['reviewed-snapshot-unavailable'])
    return result


def main():
    p = argparse.ArgumentParser(); p.add_argument('--root', default=str(Path(__file__).resolve().parents[1]))
    sub = p.add_subparsers(dest='command', required=True)
    prep = sub.add_parser('prepare'); prep.add_argument('--input', required=True); prep.add_argument('--output', required=True); prep.add_argument('--worker', required=True)
    pub = sub.add_parser('publish'); pub.add_argument('--proposal', required=True); pub.add_argument('--approval', required=True); pub.add_argument('--worker', required=True)
    read = sub.add_parser('lookup'); read.add_argument('--state', required=True); read.add_argument('--intervention', required=True)
    args = p.parse_args()
    if args.command == 'prepare': result = prepare(args.root, args.input, args.output, args.worker)
    elif args.command == 'publish':
        proposal = json.loads(tx.inside(args.root, args.proposal).read_bytes())
        data = json.loads(tx.checked(args.root, proposal['outputs']['projects.json']))
        approval = json.loads(tx.inside(args.root, args.approval).read_bytes())
        date(approval.get('reviewed_at'))
        agents_receipt = tx.read_head(args.root, AGENTS)
        if data['agents_transaction'] != agents_receipt['transaction']:
            raise ValueError('Agents changed after preparation; rebase and review')
        agents = json.loads(tx.checked(args.root, agents_receipt['outputs']['agents.json']))
        previous_receipt = tx.read_head(args.root, STORE)
        previous = json.loads(tx.checked(args.root, previous_receipt['outputs']['projects.json'])) if previous_receipt else None
        # An exact activated retry has already validated its original parent.
        if not previous_receipt or previous_receipt['proposal']['sha256'] != tx.sha(tx.inside(args.root, args.proposal).read_bytes()):
            validate(data, json.loads(tx.checked(args.root, data['cohort'])), agents, previous)
        if graph_bytes(data) != tx.checked(args.root, proposal['outputs']['projects.ttl']):
            raise ValueError('RDF and JSON snapshots disagree')
        with tx.locked(tx.inside(args.root, AGENTS)):
            if tx.read_head(args.root, AGENTS)['transaction'] != data['agents_transaction']:
                raise ValueError('Agents changed during publication')
            result = tx.publish(args.root, STORE, args.proposal, args.approval, args.worker)
    else: result = lookup(args.state, args.intervention, root=args.root)
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == '__main__': main()
