"""Verified study/document/finding metadata, independent of registry refreshes.

Turtle is authoritative. The private coordinator uses locked, recoverable file
transactions; workflow bookkeeping is separate from the RDF knowledge.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
try:
    from scripts import rdf_store
except ModuleNotFoundError:
    import rdf_store
StoreError = rdf_store.StoreError
import time
from typing import Any, Mapping
from urllib.parse import urlsplit

from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import RDF, RDFS, SKOS, XSD, DCTERMS

ROOT = Path(__file__).resolve().parents[1]
VOCABULARY_ROOT=Path(os.getenv('UE_VOCABULARY_ROOT',ROOT/'vocabularies'))
UE = Namespace('https://universalevidence.com/ontology/')
PICO = Namespace('http://data.cochrane.org/ontologies/pico/')
PROV = Namespace('http://www.w3.org/ns/prov#')
OBO = Namespace('http://purl.obolibrary.org/obo/')
SCHEMA = 'study-enrichment-1'
SOURCES = {'ctgov': 'CT.gov', 'isrctn': 'ISRCTN', 'aea': 'AEA', 'who-ictrp': 'WHO ICTRP'}
DISPOSITIONS = {'results-found', 'no-report-found', 'inaccessible', 'ambiguous-study-match', 'partial', 'evidence-blocked'}
STATUSES = {'active', 'superseded', 'retracted'}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def initialize(conn):
    if conn.schema != SCHEMA: raise ValueError('Unsupported enrichment schema')


def writer(path):
    return rdf_store.transaction(path,SCHEMA,export_rdf)


def open_store(path):
    return rdf_store.Store(path,SCHEMA,export_rdf)


def records(conn):
    return [(s['id'],r) for s in conn.data['studies'].values() for r in s['records']]


def findings_for(conn,identity):
    return [deepcopy(f) for _,f in sorted(conn.data['findings'].items()) if f['study_id']==identity]


def documents_for(conn,identity):
    return [deepcopy(d) for _,d in sorted(conn.data['documents'].items()) if any(l['study_id']==identity for l in d['study_links'])]


def axis_index(conn):
    rows=set()
    for identity,study in conn.data['studies'].items():
        for axis in study.get('axes',[]):
            if _get(conn,'documents',axis['source']['document_id'])['status']=='active':rows.add((identity,axis['axis'],axis['uri'],''))
        for f in findings_for(conn,identity):
            if active_finding(conn,f):rows.add((identity,'outcome',f['state_uri'],f['id']))
    return sorted(rows)


def _required(value, fields, description):
    if not isinstance(value, dict) or any(value.get(key) in (None, '', [], {}) for key in fields):
        raise ValueError(f'{description} requires {", ".join(fields)}')


def _fields(value, allowed, label):
    if not isinstance(value,dict) or set(value)-set(allowed): raise ValueError('Unsupported '+label+' fields')


def _url(value):
    parts = urlsplit(str(value))
    if parts.scheme not in {'http', 'https'} or not parts.netloc or parts.username or parts.password:
        raise ValueError('Expected a public HTTP(S) identifier or source URL')


def _proof(proof):
    _required(proof, ('basis', 'source_url', 'locator', 'checked_by', 'checked_at'), 'Identity verification')
    if proof['basis'] not in {'explicit-registry-id', 'verified-match'}:
        raise ValueError('Title similarity is not verified study identity')
    _fields(proof,('basis','source_url','locator','checked_by','checked_at','rationale'),'identity proof')
    _url(proof['source_url'])
    datetime.fromisoformat(proof['checked_at'].replace('Z', '+00:00'))
    if proof['basis'] == 'verified-match' and not proof.get('rationale'):
        raise ValueError('Verified match needs documented corroboration')


@lru_cache(maxsize=4)
def _taxonomy(filename, modified, vocabulary_root):
    root = Path(vocabulary_root)
    graph=Graph().parse(root / filename, format='turtle')
    if filename == 'states.ttl' and (root / 'indicators.ttl').exists():
        graph.parse(root / 'indicators.ttl', format='turtle')
    if filename=='regions.ttl':
        for name in ('geonames-countries-mirror.ttl','geonames-adm1-mirror.ttl'):
            path=root/'mirrors'/name
            if path.exists(): graph.parse(path,format='turtle')
    return graph


def taxonomy(filename):
    path = VOCABULARY_ROOT / filename
    dependencies = [path]
    if filename == 'states.ttl':
        dependencies.append(VOCABULARY_ROOT / 'indicators.ttl')
    revisions = tuple((str(p), p.stat().st_mtime_ns if p.exists() else None) for p in dependencies)
    return _taxonomy(filename, revisions, str(VOCABULARY_ROOT.resolve()))


def concept_matches(uri, root, axis):
    filename = 'states.ttl' if axis in {'state', 'condition', 'outcome'} else f'{axis}s.ttl'
    graph = taxonomy(filename)
    seen, pending = set(), [URIRef(uri)]
    while pending:
        node = pending.pop()
        if node in seen:
            continue
        if str(node) == root:
            return True
        seen.add(node)
        pending.extend(graph.objects(node, SKOS.broader))
        pending.extend(graph.subjects(SKOS.narrower, node))
    return False


def _mapped_state(finding):
    graph = taxonomy('states.ttl')
    state = URIRef(finding['state_uri'])
    if (state, RDF.type, UE.State) not in graph:
        raise ValueError('Finding requires a canonical State')
    indicator = finding.get('indicator_uri')
    if indicator and (URIRef(indicator), UE.measures, state) not in graph and (state, UE.measuredBy, URIRef(indicator)) not in graph:
        raise ValueError('Indicator has no verified measures relationship to this State')
    _required(finding.get('mapping'), ('basis', 'rationale'), 'Outcome mapping')
    if finding['mapping']['basis'] not in {'explicit-state', 'indicator-measures-state'}:
        raise ValueError('Search membership/broad growth is not outcome mapping evidence')
    if finding['mapping']['basis'] == 'indicator-measures-state' and not indicator:
        raise ValueError('Indicator mapping requires an indicator')


def _get(conn,table,identity):
    if conn.read_deadline < time.monotonic(): raise StoreError('enrichment_read_budget')
    return deepcopy(conn.data[table].get(identity))


def revision_preconditions(conn,study_id):
    result={'studies':{},'documents':{},'findings':{}}
    study=_get(conn,'studies',study_id)
    if study:result['studies'][study_id]=digest(study)
    for d in documents_for(conn,study_id):result['documents'][d['id']]=digest(d)
    for f in findings_for(conn,study_id):result['findings'][f['id']]=digest(f)
    return result


def _review(review):
    _required(review,('reviewed_by','reviewed_at','rationale'),'Explicit change review')
    return _date(review['reviewed_at'])


def _date(value):
    parsed=datetime.fromisoformat(value.replace('Z','+00:00'))
    if parsed.tzinfo is None: raise ValueError('Revision dates require an explicit timezone')
    return parsed


def _removes(old, new):
    if isinstance(old,dict):
        return not isinstance(new,dict) or any(k not in new or _removes(v,new[k]) for k,v in old.items())
    if isinstance(old,list):
        return not isinstance(new,list) or not {canonical(v) for v in old} <= {canonical(v) for v in new}
    return old not in (None,'') and new in (None,'')


def _precondition(batch, table, old):
    if batch.get('preconditions',{}).get(table,{}).get(old['id']) != digest(old):
        raise ValueError(f'Stale or missing revision precondition: {table}/{old["id"]}')


def _transition(batch, table, old, new):
    if old['status']==new['status']: return
    review=batch.get('eligibility_reviews',{}).get(table,{}).get(old['id'])
    reviewed_at=_review(review)
    if table=='documents' and reviewed_at<_date(new['status_check']['checked_at']):
        raise ValueError('Eligibility review predates its status check')
    if review.get('from')!=old['status'] or review.get('to')!=new['status']:
        raise ValueError('Eligibility review must name the current and proposed status')


def prepare_batch(conn, batch):
    """Patch study metadata; all writes still pass revision checks in validation."""
    batch=deepcopy(batch)
    incoming=batch.get('study') or {}
    old=_get(conn,'studies',incoming.get('id'))
    if old and not batch.get('study_replacement_review'):
        merged={**old,**incoming}
        # Omission is preservation; explicit collection replacement is revision checked.
        if 'context' in incoming and isinstance(incoming['context'],dict):
            merged['context']={**old.get('context',{}),**incoming['context']}
        batch['study']=merged
    return batch


def validate_submission_contract(submission):
    """Pure preflight shared by Teacher handoff and canonical batch validation."""
    import math
    batch = submission.get('batch')
    if batch is None:
        return
    if not isinstance(batch, dict):
        raise ValueError('Extraction batch must be an object')
    from scripts.result_extraction_contract import validate_finding, validate_coverage
    validate_coverage(submission)
    for finding in batch.get('findings', []):
        validate_finding(finding)
        uncertainty = finding.get('uncertainty')
        if uncertainty is None or uncertainty == {}:
            continue
        label = str(finding.get('id', 'finding'))
        if not isinstance(uncertainty, dict):
            raise ValueError(f'{label}: uncertainty must be an object')
        if 'type' in uncertainty or not isinstance(uncertainty.get('kind'), str) or not uncertainty['kind']:
            raise ValueError(f'{label}: uncertainty requires kind, not type; review and reseal legacy payloads')
        kind = uncertainty['kind']
        def number(key):
            value = uncertainty.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f'{label}: uncertainty {key} must be a finite reported number')
            return value
        if kind == 'standard-error':
            if number('value') < 0:
                raise ValueError(f'{label}: standard error must be nonnegative')
        elif kind == 'confidence-interval':
            if number('lower') > number('upper'):
                raise ValueError(f'{label}: confidence interval bounds are reversed')
            if not 0 < number('level') < 100:
                raise ValueError(f'{label}: confidence level must be a reported percentage between 0 and 100')
        # Other reported dispersion kinds remain metadata; consumer readiness is
        # checked independently. Never reinterpret SD/IQR as inferential uncertainty.


def validate_batch(conn, batch):
    """Validate identity, field-level evidence, taxonomy, and append-only findings."""
    validate_submission_contract({"batch": batch})
    batch=prepare_batch(conn,batch)
    if batch.get('schema') != SCHEMA:
        raise ValueError('Unknown extraction contract')
    if set(batch) - {'schema', 'study', 'documents', 'findings', 'preconditions', 'eligibility_reviews', 'study_replacement_review'}:
        raise ValueError('Extraction stores metadata only; unknown batch fields')
    study = batch.get('study')
    _required(study, ('id', 'records'), 'Underlying study')
    _url(study['id'])
    if len(canonical(batch)) > 2_000_000:
        raise ValueError('Metadata batch exceeds bounded size; do not persist report text')
    allowed_study = {'id', 'records', 'title', 'year', 'country', 'axes', 'context', 'planned_outcomes'}
    if set(study) - allowed_study:
        raise ValueError('Unknown study metadata field')
    existing_findings={f['id']:canonical(f) for f in findings_for(conn,study['id'])}
    existing_findings.update({f['id']:canonical(f) for f in batch.get('findings',[])})
    if len(existing_findings)>500 or sum(len(v) for v in existing_findings.values())+len(canonical(study))>2_000_000:
        raise ValueError('Study metadata cap exceeded (500 finding versions / 2MB); retain unresolved work for review')
    if len(study['records'])>20 or len(study.get('axes',[]))>100 or len(study.get('planned_outcomes',[]))>100:
        raise ValueError('Study context cap exceeded')
    old_study = _get(conn, 'studies', study['id'])
    if old_study and old_study!=study:
        _precondition(batch,'studies',old_study)
        removed=_removes(old_study,study)
        if removed or batch.get('study_replacement_review'):
            _review(batch.get('study_replacement_review'))
    new_keys = set()
    for record in study['records']:
        _required(record, ('source', 'record_id', 'url', 'verification'), 'Registry link')
        if record['source'] not in SOURCES:
            raise ValueError('Report publishers are not registry sources')
        _fields(record,('source','record_id','record_uri','url','verification'),'registry metadata')
        if record.get('record_uri'): _url(record['record_uri'])
        _url(record['url']); _proof(record['verification'])
        key = (record['source'], record['record_id'])
        if key in new_keys:
            raise ValueError('Duplicate registry identity')
        new_keys.add(key)
        for linked_study,old_record in records(conn):
            if old_record['source']!=record['source']:continue
            if record.get('record_uri') and old_record.get('record_uri')==record['record_uri'] and old_record['record_id']!=record['record_id']:
                raise ValueError('Registry URI already has another source identifier')
            if old_record['record_id']==record['record_id']:
                if old_record.get('record_uri') and old_record['record_uri']!=record.get('record_uri'):raise ValueError('Original registry URI is immutable')
                if linked_study!=study['id']:raise ValueError('Registry identity already belongs to another study; reviewed reconciliation required')
    if old_study and not {(r['source'], r['record_id']) for r in old_study['records']} <= new_keys:
        raise ValueError('Do not remove original registry identifiers')
    versions=[(d['identifier'],d['version']) for d in batch.get('documents',[])]
    if len(set(versions)) != len(versions): raise ValueError('Duplicate document identifier/version')
    documents = {d['id']: d for d in batch.get('documents', [])}
    if len(documents) != len(batch.get('documents', [])):
        raise ValueError('Duplicate document version')
    doc_fields = {'id', 'identifier', 'version', 'kind', 'citation', 'url', 'status', 'status_check', 'study_links', 'replaces', 'discovery'}
    if len(documents)>100: raise ValueError('Document batch cap exceeded')
    for doc in documents.values():
        if len(canonical(doc))>16000 or len(doc.get('study_links',[]))>20:raise ValueError('Document metadata cap exceeded')
        _required(doc, ('id', 'identifier', 'version', 'kind', 'citation', 'url', 'status', 'status_check', 'study_links'), 'Document')
        if set(doc) - doc_fields:
            raise ValueError('Unknown document field; full text/PDFs are not stored')
        _url(doc['url']); _url(doc['id'])
        for step in doc.get('discovery',[]):
            _required(step,('source_url','accessed_url','method','version_relation','checked_at','rationale'),'Report discovery provenance')
            _url(step['source_url']);_url(step['accessed_url']);_date(step['checked_at'])
            if step.get('linked_url'):_url(step['linked_url'])
            if step['method'] not in {'registry-link','alternate-copy-search','version-search','other'}:raise ValueError('Unknown discovery method')

        if doc['status'] not in STATUSES:
            raise ValueError('Unknown report eligibility')
        if doc['kind'] not in {'registration', 'protocol', 'registry-results', 'article', 'correction', 'follow-up',
                               'thesis', 'dissertation', 'technical-report', 'sponsor-report', 'presentation', 'poster'}:
            raise ValueError('Unknown source document kind')
        _required(doc['status_check'], ('checked_at', 'source_url', 'result', 'checked_by'), 'Correction/retraction check')
        _url(doc['status_check']['source_url'])
        _date(doc['status_check']['checked_at'])
        if doc['status_check']['result'] != doc['status']:
            raise ValueError('Document status must match its checked result')
        for link in doc['study_links']:
            count=sum(d['id'] not in documents for d in documents_for(conn,link['study_id']))
            if count+len(documents)>100:raise ValueError('Study document version cap exceeded; retain unresolved work')
            _required(link, ('study_id', 'verification'), 'Document-study link')
            _proof(link['verification'])
            if link['study_id'] != study['id'] and not _get(conn, 'studies', link['study_id']):
                raise ValueError('Document links an unknown study')
        if study['id'] not in {link['study_id'] for link in doc['study_links']}:
            raise ValueError('Document not verified for claimed study')
        old = _get(conn, 'documents', doc['id'])
        if old and old!=doc:
            _precondition(batch,'documents',old)
            checked=_date(doc['status_check']['checked_at']);previous=_date(old['status_check']['checked_at'])
            if checked<previous or (doc['status']!=old['status'] and checked<=previous):
                raise ValueError('Stale report status check; a transition requires a newer check')
            _transition(batch,'documents',old,doc)
        if old and any(old.get(key) != doc.get(key) for key in ('identifier', 'version', 'kind')):
            raise ValueError('Document identity/version is immutable')
        if old and not {x['study_id'] for x in old['study_links']} <= {x['study_id'] for x in doc['study_links']}:
            raise ValueError('Do not erase another study described by the same paper')
        if 'doi.org/' in doc['identifier'].lower(): raise ValueError('Use canonical doi: identifier, with document URL kept separately')
        if doc['identifier'].lower().startswith('doi:') and doc['identifier'] != doc['identifier'].lower():
            raise ValueError('Normalize DOI identifiers to lowercase')
        if any(d['identifier']==doc['identifier'] and d['version']==doc['version'] and d['id']!=doc['id'] for d in conn.data['documents'].values()):
            raise ValueError('Document identifier/version already exists')
    finding_fields = {'id', 'study_id', 'status', 'state_uri', 'indicator_uri', 'indicator_review', 'measure', 'mapping', 'comparison', 'subgroup', 'timing', 'value', 'unit', 'result_text', 'uncertainty', 'direction', 'significance', 'sources', 'replaces', 'superseded_by', 'lineage_review', 'derivation', 'assessment_scale', 'aggregation_method', 'endpoint', 'primary_endpoint', 'statistic_type'}
    if len({f['id'] for f in batch.get('findings',[])}) != len(batch.get('findings',[])):
        raise ValueError('Duplicate finding identity')
    _validate_context(study, documents, conn)
    for finding in batch.get('findings', []):
        _required(finding, ('id', 'study_id', 'status', 'state_uri', 'measure', 'mapping', 'comparison', 'timing', 'sources'), 'Reported finding')
        _url(finding['id'])
        if set(finding) - finding_fields:
            raise ValueError('Unsupported finding field')
        if finding['study_id'] != study['id'] or finding['status'] not in STATUSES:
            raise ValueError('Finding study/status mismatch')
        if finding.get('value') is None and not finding.get('result_text'):
            raise ValueError('Planned or unavailable results are not findings')
        if finding.get('value') is not None and (isinstance(finding['value'],bool) or not isinstance(finding['value'],(int,float))):
            raise ValueError('Value must be an original number; inequalities and unavailable values belong in result_text')
        if finding.get('result_text') and len(finding['result_text'])>2000:raise ValueError('Use a concise faithful result, not report full text')
        if finding.get('value') is not None and not finding.get('unit'):
            raise ValueError('Numerical value requires its reported unit (including dimensionless)')
        if finding.get('direction') not in {None, 'increase', 'decrease', 'no-change', 'mixed', 'uncertain'}:
            raise ValueError('Direction is quantity-relative, not benefit/harm/significance')
        _required(finding['comparison'], ('id', 'kind', 'reference', 'quantity'), 'Comparison or baseline')
        if finding['comparison']['kind'] not in {'between-groups', 'within-group', 'association', 'descriptive'}:
            raise ValueError('Specify arm-level versus between-group comparison')
        _required(finding['timing'], ('reported', 'reference_point'), 'Follow-up')
        _fields(finding['comparison'],('id','kind','reference','quantity','intervention','comparator','baseline','arm_statistics','intervention_concepts','comparator_concepts','taxonomy_rationale'),'comparison')
        _fields(finding['timing'],('reported','reference_point','age_at_assessment_months','age_statistic','age_conversion'),'timing')
        _fields(finding['mapping'],('basis','rationale'),'mapping')
        _mapped_state(finding)
        if finding.get('indicator_uri'):
            if (URIRef(finding['indicator_uri']),RDF.type,UE.Indicator) not in taxonomy('states.ttl'):
                raise ValueError('Expected an Indicator, not a State or untyped metric')
        elif not _get(conn,'findings',finding['id']):
            _required(finding.get('indicator_review'),('status','rationale','next_step'),'New finding requires indicator_uri or an explicit indicator gap review')
            if finding['indicator_review']['status']!='unresolved':raise ValueError('Unmapped indicator must remain unresolved')
        for role in ('intervention','comparator'):
            concepts=finding['comparison'].get(role+'_concepts',[])
            if concepts:
                _required(finding['comparison'],('taxonomy_rationale',),'Comparison mapping')
                if not isinstance(concepts,list) or any((URIRef(c),RDF.type,UE.Intervention) not in taxonomy('interventions.ttl') for c in concepts):
                    raise ValueError('Comparison must name canonical Intervention concepts')
        if len(finding['sources'])>10:raise ValueError('Finding source locator cap exceeded')
        supported = set()
        for source in finding['sources']:
            _required(source, ('document_id', 'locator', 'fields'), 'Finding source locator')
            doc = documents.get(source['document_id']) or _get(conn, 'documents', source['document_id'])
            if not doc or study['id'] not in {link['study_id'] for link in doc['study_links']}:
                raise ValueError('Finding has no verified supporting study-document link')
            if doc['kind'] in {'registration', 'protocol'}:
                raise ValueError('Registry plans/protocols cannot supply reported results; use registry-results version')
            _fields(source,('document_id','locator','fields'),'source locator')
            if not isinstance(source['fields'],list) or not all(isinstance(v,str) for v in source['fields']):raise ValueError('Source fields must be field names')
            supported.update(source['fields'])
        required_support = set(finding) - {'id','study_id','status','sources','mapping','replaces','superseded_by','lineage_review','derivation','state_uri','indicator_uri','indicator_review'}
        if required_support - supported:
            raise ValueError(f'Unsupported finding fields: {sorted(required_support-supported)}')
        if finding.get('derivation'):
            _required(finding['derivation'], ('method', 'inputs', 'performed_by'), 'Derived estimate')
        old = _get(conn, 'findings', finding['id'])
        if old and old!=finding:
            _precondition(batch,'findings',old)
            _transition(batch,'findings',old,finding)
        mutable = {'status', 'superseded_by', 'lineage_review'}
        if old and {k:v for k,v in old.items() if k not in mutable} != {k:v for k,v in finding.items() if k not in mutable}:
            raise ValueError('Finding facts are immutable; create a linked correction/follow-up')
        if finding.get('replaces'):
            previous = _get(conn, 'findings', finding['replaces'])
            if previous and previous.get('superseded_by')!=finding['id']:
                _precondition(batch,'findings',previous)
            if not previous or previous['study_id'] != study['id'] or previous['id']==finding['id'] or (previous['status']!='active' and previous.get('superseded_by')!=finding['id']):
                raise ValueError('Correction must identify a previous finding of this study')
    for axis in study.get('axes', []):
        _required(axis, ('axis', 'uri', 'source'), 'Verified study context axis')
        if axis['axis'] not in {'condition', 'intervention', 'region'}:
            raise ValueError('Reported outcome axes are derived from eligible findings')
        _required(axis['source'], ('document_id', 'locator'), 'Axis mapping evidence')
        doc = documents.get(axis['source']['document_id']) or _get(conn,'documents',axis['source']['document_id'])
        if not doc or study['id'] not in {x['study_id'] for x in doc['study_links']}:
            raise ValueError('Axis mapping lacks a verified study document')
        graph = taxonomy('states.ttl' if axis['axis']=='condition' else axis['axis']+'s.ttl')
        target = URIRef(axis['uri'])
        cls = {'condition':UE.State,'intervention':UE.Intervention,'region':UE.Region}[axis['axis']]
        if not any(graph.triples((target,None,None))) or (axis['axis'] != 'region' and (target,RDF.type,cls) not in graph):
            raise ValueError('Invalid canonical context mapping')
    return batch


def _validate_context(study, documents, conn):
    context=study.get('context') or {}
    if set(context)-{'population','intervention_groups','comparator_groups'}:
        raise ValueError('Unknown study context')
    for planned in study.get('planned_outcomes',[]):
        _required(planned,('measure','source'),'Planned measure')
        if set(planned)-{'measure','timing','source'}: raise ValueError('Unknown planned measure field')
        _required(planned['source'],('document_id','locator'),'Planned measure locator')
        doc=documents.get(planned['source']['document_id']) or _get(conn,'documents',planned['source']['document_id'])
        if not doc or study['id'] not in {link['study_id'] for link in doc['study_links']}:raise ValueError('Planned measure needs verified document')
    for item in ([context['population']] if context.get('population') else []) + context.get('intervention_groups',[]) + context.get('comparator_groups',[]):
        _required(item, ('source',), 'Context source')
        _required(item['source'], ('document_id','locator'), 'Context locator')
        doc=documents.get(item['source']['document_id']) or _get(conn,'documents',item['source']['document_id'])
        if not doc or study['id'] not in {link['study_id'] for link in doc['study_links']}:
            raise ValueError('Context requires verified source document')
        allowed={'kind','description','age','condition','source'} if item is context.get('population') else {'id','label','concept_uri','dose','duration','schedule','modeOfDelivery','source'}
        if set(item)-allowed: raise ValueError('Unknown context descriptor')
        if item is context.get('population'):
            _required(item,('kind','description'),'Population')
            if item['kind'] not in {'human','non-human'}: raise ValueError('Population scope required')
        else: _required(item,('id','label'),'Study arm')


def active_finding(conn, finding):
    if finding['status'] != 'active':
        return False
    states = [_get(conn,'documents',s['document_id'])['status'] for s in finding['sources']]
    if all(status == 'active' for status in states):
        return True
    review = finding.get('lineage_review') or {}
    return (any(status == 'active' for status in states)
            and review.get('approved') is True and all(review.get(k) for k in ('reviewed_by','reviewed_at','rationale'))
            and set().union(*(set(source['fields']) for source,status in zip(finding['sources'],states) if status=='active')) >= set(review.get('supported_fields',[]))
            and set(review.get('supported_fields',[])) >= (set(finding)-{'id','study_id','status','sources','mapping','replaces','superseded_by','lineage_review','derivation','state_uri','indicator_uri','indicator_review'})
            and set(review.get('active_document_ids', [])) == {
                source['document_id'] for source,status in zip(finding['sources'],states) if status=='active'
            })


def apply_batch(conn,batch):
    """Inside the same locked file transaction as the coordinator receipt."""
    initialize(conn);batch_hash=digest(batch)
    if batch_hash in conn.data['events']:return batch_hash
    submitted=deepcopy(batch);batch=validate_batch(conn,batch)
    study=batch['study'];conn.data['studies'][study['id']]=deepcopy(study)
    for doc in batch.get('documents',[]):conn.data['documents'][doc['id']]=deepcopy(doc)
    for finding in batch.get('findings',[]):
        conn.data['findings'][finding['id']]=deepcopy(finding)
        if finding.get('replaces'):
            conn.data['findings'][finding['replaces']].update(status='superseded',superseded_by=finding['id'])
    conn.data['events'][batch_hash]={'id':batch_hash,'batch':submitted,'created_at':datetime.now(timezone.utc).isoformat()}
    conn.meta['version']=digest([conn.meta['version'],batch_hash])
    return batch_hash


@contextmanager
def reader(path=None,budget=2.0):
    path=path or os.getenv('UE_ENRICHMENT_TTL')
    if not path:yield None;return
    with rdf_store.snapshot(path,SCHEMA,export_rdf,budget) as store:yield store


def limit():
    return max(1,min(1000,int(os.getenv('UE_ENRICHMENT_LIMIT','100'))))


def snapshot_version(conn):
    return digest([conn.meta['version'],conn.revision,limit()])


def version():
    try:
        with reader() as conn:
            return snapshot_version(conn) if conn else 'disabled'
    except (OSError,StoreError,TypeError,ValueError):
        return 'unavailable'


def resolve(conn,source,record_id):
    return next((sid for sid,r in records(conn) if r['source']==source and record_id in (r['record_id'],r.get('record_uri'))),None)


def public_study(conn, identity):
    study=_get(conn,'studies',identity)
    active,history=[],[]
    for finding in findings_for(conn,identity):
        eligible=active_finding(conn,finding)
        finding['citations']=[{**_get(conn,'documents',s['document_id']),'locator':s['locator']} for s in finding['sources']]
        finding['eligibility']=('active' if eligible else finding['status'] if finding['status']!='active' else 'withdrawn-source-support')
        (active if eligible else history).append(finding)
    return {'study_identity':identity,'registry_records':study['records'],
            'reported_findings':active,'finding_history':history,
            'planned_outcomes':study.get('planned_outcomes',[]),
            'preconditions':revision_preconditions(conn,identity)}


def _roots_for(uri, axis, roots):
    return [root for root in roots if concept_matches(uri,root,axis)]


def retrieve(conn, query):
    """RDF-derived outcome candidate window, capped before metadata expansion."""
    cap=limit()
    roots=tuple(set(query.values['state']).union(query.values['outcome']))
    if not roots:
        return [],dict(status='included',limit=cap,returned_unique_studies=0,truncated=False,approximate=False)
    # Resolve direct State URIs through taxonomy descendants in this RDF snapshot.
    # No registry corpus scan or secondary persistent index is used.
    graph=taxonomy('states.ttl');descendants=set()
    for root in roots:
        pending=[URIRef(root)]
        while pending:
            node=pending.pop()
            if str(node) in descendants: continue
            descendants.add(str(node));pending.extend(graph.subjects(SKOS.broader,node));pending.extend(graph.objects(node,SKOS.narrower))
    index=axis_index(conn)
    candidates=[(sid,) for sid in sorted({sid for sid,axis,uri,fid in index if axis=='outcome' and uri in descendants})][:cap+1]
    truncated=len(candidates)>cap;rows=[]
    for identity, in candidates[:cap]:
        study=_get(conn,'studies',identity)
        axis_values={axis:[] for axis in ('condition','intervention','outcome','region')}
        for axis,uri in sorted({(axis,uri) for sid,axis,uri,fid in index if sid==identity}):
            axis_values[axis].append(uri)
        axis_values['state']=axis_values['condition']+axis_values['outcome']
        axis_values['region'].append('https://universalevidence.com/vocab/regions/World')
        matched={axis:{root for uri in axis_values[axis] for root in _roots_for(uri,axis,query.values[axis])} for axis in axis_values}
        if any(query.values[axis] and not (all(root in matched[axis] for root in query.values[axis]) if query.logic[axis]=='and' else bool(matched[axis])) for axis in axis_values):
            continue
        findings=public_study(conn,identity)
        outcome_roots={root for uri in axis_values['outcome'] for root in _roots_for(uri,'state',query.values['state'])}
        matches=[{'selected_root':root,'retrieval_basis':'reported_outcome','retrieval_role':'outcome',
                  'finding_ids':sorted(f['id'] for f in findings['reported_findings'] if concept_matches(f['state_uri'],root,'state'))} for root in sorted(outcome_roots)]
        condition_roots={root for uri in axis_values['condition'] for root in _roots_for(uri,'condition',query.values['state'])}
        matches.extend({'selected_root':root,'retrieval_basis':'mapping','retrieval_role':'condition','retrieval_source':'enrichment_context'} for root in sorted(condition_roots))
        direct_mappings=[{'uri':uri,'role':'outcome','provenance':'reported_outcome','finding_ids':sorted(f['id'] for f in findings['reported_findings'] if f['state_uri']==uri)} for uri in sorted(axis_values['outcome'])]
        direct_mappings.extend({'uri':uri,'role':'condition','provenance':'verified_report_context'} for uri in sorted(axis_values['condition']))
        interventions=[uri for uri in axis_values['intervention'] if not query.values['intervention'] or _roots_for(uri,'intervention',query.values['intervention'])]
        for record in study['records']:
            for intervention in interventions or [None]:
                rows.append({'source':SOURCES[record['source']],'study_id':record['record_id'],
                    'title':study.get('title'),'url':record['url'],'year':study.get('year'),'country':study.get('country'),
                    'intervention_concept_uri':intervention,
                    'intervention_concept':str(next(taxonomy('interventions.ttl').objects(URIRef(intervention),SKOS.prefLabel),'')) if intervention else None,
                    'outcomes':[],'query_matches':matches,'direct_state_mappings':direct_mappings,
                    '_reported_state_uris':axis_values['outcome'],
                    'match_provenance':[{'basis':'reported_outcome','finding_ids':[f['id'] for f in findings['reported_findings']]}],**findings})
    return rows,dict(status='included',limit=cap,returned_unique_studies=len({r['study_identity'] for r in rows}),truncated=truncated,approximate=truncated,reason='enrichment_limit' if truncated else None)


def source_outcome_kind(outcome):
    if not isinstance(outcome,dict): return 'unspecified'
    labels={str(outcome.get(k) or '').lower().replace('_','-') for k in ('type','outcome_status','outcomeStatus')}
    if labels & {'reported-result','adverse-event','reported','synthesized'}:return 'reported'
    if labels & {'pre-specified-measure','pre-specified','planned','primary','secondary','other','primary-outcome','secondary-outcome'}:return 'planned'
    return 'unspecified'


def normalize_source_outcomes(row):
    row=dict(row)
    for key,kind in [('planned_outcomes','planned'),('source_reported_outcomes','reported'),('source_unspecified_outcomes','unspecified')]:
        values=list(row.get(key) or [])
        values.extend(value for value in row.get('outcomes') or [] if source_outcome_kind(value)==kind)
        row[key]=list({canonical(v):v for v in values}.values())
    return row


def decorate(conn, rows, source=None):
    result=[]
    for raw in rows:
        row=normalize_source_outcomes(raw);sid=source or next((key for key,label in SOURCES.items() if label==row.get('source')),row.get('source'))
        identity=row.get('study_identity') or resolve(conn,sid,str(row.get('_graph_support_id') or row.get('study_id') or ''))
        if identity:
            details=public_study(conn,identity)
            details['planned_outcomes']=list({canonical(v):v for v in [*details['planned_outcomes'],*(row.get('planned_outcomes') or [])]}.values())
            row.update(details)
        result.append(row)
    return result


def merge_rows(existing, incoming):
    """Retain source metadata and plans when a verified identity joins rows."""
    result={**incoming,**{k:v for k,v in existing.items() if v not in (None,'',[])}}
    for field in ('outcomes','planned_outcomes','source_reported_outcomes','source_unspecified_outcomes','registry_records','reported_findings','finding_history','match_provenance','query_matches','direct_state_mappings'):
        if field in existing or field in incoming:
            result[field]=list({canonical(v):v for v in [*(existing.get(field) or []),*(incoming.get(field) or [])]}.values())
    return result


def enrich_search(payload, query):
    if not os.getenv('UE_ENRICHMENT_TTL'):
        return payload
    try:
        with reader() as conn:
            extra,metadata=retrieve(conn,query)
            rows=decorate(conn,payload['results'])+extra
            grouped={}
            for row in rows:
                key=(row.get('study_identity') or (row.get('source'),row.get('study_id')),row.get('intervention_concept_uri') or row.get('intervention'),row.get('condition_concept_uri'))
                if key not in grouped: grouped[key]=row
                else:
                    grouped[key]=merge_rows(grouped[key],row)
            plans={}
            for row in grouped.values():
                if row.get('study_identity'):
                    plans.setdefault(row['study_identity'],{}).update({canonical(v):v for v in row.get('planned_outcomes',[])})
            for row in grouped.values():
                if row.get('study_identity'):row['planned_outcomes']=list(plans[row['study_identity']].values())
            result={**payload,'results':list(grouped.values()),'meta':dict(payload['meta'])}
            result['meta']['enrichment']=metadata
            result['meta']['enrichment_version']=snapshot_version(conn)
            result['meta']['returned_unique_studies']=len({r.get('study_identity') or (r.get('source'),r.get('study_id')) for r in rows})
            result['meta']['source_counts_overlap']=any(r.get('study_identity') for r in rows)
            result['meta']['sources']={source:{**m,'registry_returned_unique_studies':m.get('returned_unique_studies',0),'enrichment_returned_unique_studies':len({r['study_identity'] for r in extra if r['source']==SOURCES[source]}),'returned_unique_studies':len({r.get('study_identity') or r.get('study_id') for r in rows if r.get('source')==SOURCES[source]})} for source,m in result['meta']['sources'].items()}
            result['meta']['truncated'] |= metadata['truncated'];result['meta']['approximate'] |= metadata['approximate']
            if metadata['approximate'] or (result['results'] and result['meta'].get('execution_status')=='timeout'):
                result['meta']['execution_status']='partial'
            return result
    except (OSError,StoreError,ValueError,TypeError) as exc:
        return {**payload,'meta':{**payload['meta'],'enrichment':{'status':'unavailable','reason':type(exc).__name__},'execution_status':'partial','truncated':True,'approximate':True}}


def backup(source,destination):
    return rdf_store.backup(source,destination,SCHEMA,export_rdf)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['init','validate','commit','review','backup','export-rdf'])
    parser.add_argument('--store',required=True);parser.add_argument('--batch');parser.add_argument('--output')
    args=parser.parse_args()
    if args.command=='backup':print(backup(args.store,args.output));return
    if args.command in {'init','commit'}:
        with writer(args.store) as conn:
            if args.command=='commit':print(apply_batch(conn,json.loads(Path(args.batch).read_text())))
    else:
        with reader(args.store,budget=None) as conn:
            if args.command=='validate':validate_batch(conn,json.loads(Path(args.batch).read_text()))
            elif args.command=='review':print(canonical(review_store(conn)))
            elif args.command=='export-rdf':export_rdf(conn).serialize(args.output,format='turtle')


def graph_population(query, live_rows=None, local_rows=None):
    """Read one snapshot for independent retrieval and bounded identity lookups."""
    live={key:list(rows) for key,rows in (live_rows or {}).items()}
    identities={}
    metadata={'status':'disabled','truncated':False,'approximate':False}
    if not os.getenv('UE_ENRICHMENT_TTL'): return live,identities,metadata
    try:
        with reader() as conn:
            extra,metadata=retrieve(conn,query)
            for row in extra:
                source=next(key for key,label in SOURCES.items() if label==row['source'])
                live.setdefault(source,[]).append(row)
            for source,rows in live.items():
                live[source]=decorate(conn,rows,source)
                for row in live[source]:
                    if row.get('study_identity'):identities[(source,row['study_id'])]=row['study_identity']
            for source,rows in (local_rows or {}).items():
                for row in rows:
                    for record in str(row.get('studyUris') or '').split('|||'):
                        identity=resolve(conn,source,record)
                        if identity:identities[(source,record)]=identity
            metadata['version']=snapshot_version(conn)
            return live,identities,metadata
    except (OSError,StoreError,ValueError,TypeError) as exc:
        return live,identities,{'status':'unavailable','reason':str(exc) if str(exc)=='enrichment_read_budget' else type(exc).__name__,'version':version(),'truncated':True,'approximate':True}


def validate_claim(work,batch):
    claimed={(next((key for key,label in SOURCES.items() if label==r['source']),r['source']),str(r['study_id'])) for r in work['records']}
    submitted={(r['source'],r['record_id']) for r in batch['study']['records']}
    if not claimed <= submitted: raise ValueError('Batch does not preserve the claimed registry records')
    if work['identity'].startswith('http') and work['identity']!=batch['study']['id']:
        raise ValueError('Batch changes the claimed verified study identity')


def review_store(conn):
    result=[]
    for identity in sorted(conn.data['studies']):
        study=_get(conn,'studies',identity)
        findings=findings_for(conn,identity)
        errors=[]
        try: validate_batch(conn,{'schema':SCHEMA,'study':study,'documents':[],'findings':findings})
        except ValueError as exc: errors.append(str(exc))
        result.append({**public_study(conn,identity),'validation_errors':errors})
    return {'studies':result,'independent_review_required':True}


def export_rdf(conn):
    """Metadata export using small, explicitly selected external vocabulary terms."""
    g=Graph()
    for prefix,ns in [('ue',UE),('pico',PICO),('prov',PROV),('obo',OBO),('dct',DCTERMS)]: g.bind(prefix,ns)
    def literal(subject,predicate,value):
        if value is not None: g.add((subject,predicate,Literal(value if isinstance(value,(str,int,float,bool)) else canonical(value))))
    for identity,study in sorted(conn.data['studies'].items()):
        node=URIRef(identity)
        g.add((node,RDF.type,UE.Study)); literal(node,DCTERMS.title,study.get('title'))
        for record in study['records']:
            record_node=URIRef(record.get('record_uri') or record['url'])
            g.add((record_node,RDF.type,UE.Evidence));g.add((record_node,UE.studyIdentity,node))
            literal(record_node,DCTERMS.identifier,record['record_id'])
            literal(record_node,UE.studyId,record['record_id'])
            g.add((record_node,UE.source,URIRef('https://universalevidence.com/vocab/sources/'+record['source'])))
            literal(record_node,UE.identityVerification,record['verification'])
        context=study.get('context') or {}
        if context:
            question=URIRef(identity+'/question');g.add((node,UE.studyContext,question));g.add((question,RDF.type,PICO.PICO))
            population=context.get('population')
            if population:
                pop=URIRef(identity+'/population')
                human=population.get('kind')=='human'
                g.add((question,PICO.population if human else UE.nonHumanPopulation,pop))
                g.add((pop,RDF.type,PICO.Population if human else UE.NonHumanPopulation))
                literal(pop,RDFS.label,population['description'])
                for field,cls in [('age',PICO.Age),('condition',PICO.Condition)]:
                    if human and population.get(field):
                        value=URIRef(str(pop)+'/'+field);g.add((pop,PICO[field],value));g.add((value,RDF.type,cls));literal(value,RDFS.label,population[field])
            for role in ('intervention','comparator'):
                for i,group in enumerate(context.get(role+'_groups',[])):
                    group_node=URIRef(identity+'/'+role+'-group/'+str(i));g.add((question,PICO[role+'Group'],group_node));g.add((group_node,RDF.type,PICO.InterventionGroup));literal(group_node,RDFS.label,group['label'])
                    descriptor=URIRef(str(group_node)+'/intervention');g.add((group_node,PICO.intervention,descriptor));g.add((descriptor,RDF.type,PICO.Intervention))
                    if group.get('concept_uri'): g.add((descriptor,UE.taxonomyConcept,URIRef(group['concept_uri'])))
                    for field,cls in [('dose',PICO.Dose),('duration',PICO.Duration),('schedule',PICO.Schedule),('modeOfDelivery',PICO.ModeOfDelivery)]:
                        if group.get(field):
                            value=URIRef(str(descriptor)+'/'+field);g.add((descriptor,PICO[field],value));g.add((value,RDF.type,cls));literal(value,RDFS.label,group[field])
    for identity,doc in sorted(conn.data['documents'].items()):
        node=URIRef(identity);g.add((node,RDF.type,UE.ReportDocument))
        literal(node,DCTERMS.identifier,doc['identifier']);literal(node,DCTERMS.bibliographicCitation,doc['citation']);g.add((node,DCTERMS.source,URIRef(doc['url'])))
        literal(node,DCTERMS.type,doc['kind']);literal(node,DCTERMS.hasVersion,doc['version']);literal(node,UE.reportStatus,doc['status']);literal(node,UE.statusCheck,doc['status_check'])
        for link in doc['study_links']:
            g.add((node,OBO.IAO_0000136,URIRef(link['study_id'])));literal(node,UE.identityVerification,link)
    for identity,finding in sorted(conn.data['findings'].items()):
        node=URIRef(identity);g.add((node,RDF.type,UE.OutcomeMeasurement))
        g.add((node,UE.measurementStudy,URIRef(finding['study_id'])));g.add((node,UE.ofState,URIRef(finding['state_uri'])))
        if finding.get('indicator_uri'): g.add((node,UE.ofIndicator,URIRef(finding['indicator_uri'])))
        for role,predicate in [('intervention',UE.comparisonIntervention),('comparator',UE.comparisonComparator)]:
            for concept in finding['comparison'].get(role+'_concepts',[]):g.add((node,predicate,URIRef(concept)))
        literal(node,UE.outcomeStatus,'reported')
        if finding.get('direction'):
            literal(node,UE.observedDirection,{'increase':'positive','decrease':'negative','no-change':'null','mixed':'mixed','uncertain':'uncertain'}[finding['direction']])
        literal(node,UE.measurementStatus,finding['status']);literal(node,UE.activeForRetrieval,active_finding(conn,finding))
        for field in ('measure','comparison','subgroup','timing','value','unit','result_text','uncertainty','direction','significance','derivation','mapping','indicator_review'):
            literal(node,UE[field],finding.get(field))
        specification=URIRef(identity+'/specification');g.add((node,UE.outcomeSpecification,specification));g.add((specification,RDF.type,UE.OutcomeSpecification))
        literal(specification,RDFS.label,finding['measure'])
        group=URIRef(identity+'/outcome-group');g.add((group,RDF.type,PICO.OutcomeGroup));g.add((group,PICO.outcome,specification));g.add((node,UE.specificationGroup,group))
        question=URIRef(finding['study_id']+'/question');g.add((question,RDF.type,PICO.PICO));g.add((URIRef(finding['study_id']),UE.studyContext,question));g.add((question,PICO.outcomeGroup,group))
        for field,prop,cls in [('assessment_scale',PICO.outcomeMeasurement,PICO.AssessmentScale),('aggregation_method',PICO.aggregationMethod,PICO.AggregationMethod)]:
            if finding.get(field):
                value=URIRef(identity+'/'+field);g.add((specification,prop,value));g.add((value,RDF.type,cls));literal(value,RDFS.label,finding[field])
        for field,prop in [('endpoint',PICO.endpoint),('primary_endpoint',PICO.primaryEndpoint)]:
            if finding.get(field):
                group=URIRef(identity+'/outcome-group');g.add((group,RDF.type,PICO.OutcomeGroup));g.add((node,UE.specificationGroup,group))
                value=URIRef(identity+'/'+field);g.add((group,prop,value));g.add((value,RDF.type,PICO.Duration));literal(value,RDFS.label,finding[field])
        if finding.get('value') is not None:
            estimate=URIRef(identity+'/estimate');g.add((node,UE.estimate,estimate));g.add((estimate,RDF.type,OBO.STATO_0000039));literal(estimate,UE.reportedValue,finding['value']);literal(estimate,UE.reportedUnit,finding['unit']);literal(estimate,DCTERMS.type,finding.get('statistic_type'))
            if isinstance(finding.get('uncertainty'),dict) and finding['uncertainty'].get('kind')=='confidence-interval':
                interval=URIRef(identity+'/interval');g.add((estimate,UE.uncertaintyInterval,interval));g.add((interval,RDF.type,OBO.STATO_0000196));literal(interval,UE.reportedInterval,finding['uncertainty'])
        if finding.get('replaces'): g.add((node,PROV.wasRevisionOf,URIRef(finding['replaces'])))
        for i,source in enumerate(finding['sources']):
            doc=URIRef(source['document_id']);g.add((node,PROV.wasDerivedFrom,doc))
            locator=URIRef(identity+'/source/'+str(i));g.add((node,UE.sourceLocator,locator));g.add((locator,DCTERMS.source,doc));literal(locator,DCTERMS.description,source['locator']);literal(locator,UE.supportedFields,source['fields'])
    try:
        from scripts.cids_outcomes import add_cids
    except ModuleNotFoundError:
        from cids_outcomes import add_cids
    add_cids(g,conn.data['findings'])
    return g


if __name__=='__main__':
    main()

