"""Evidence-bound organization follow-up publications through Teacher's adapter bridge.

Each exact assignment publishes an immutable supplemental record. Original frozen
snapshots are retained. Consumers require the matching durable coordinator receipt;
uncommitted files cannot become public facts. No names are resolved by string match.
"""
import argparse
import copy
import hashlib
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import reviewed_snapshot as tx
from scripts.study_projects import refs, support, date, iri

CHECK = 'organization_evidence_followup'
STORE = 'evidence/organization-followups'
TYPE = 'https://universalevidence.com/vocab/organization-types/'
ROLE = 'https://universalevidence.com/vocab/agent-roles/'
_executing = False
BASES = {'direct-stunting-evidence', 'explicit-stunting-objective', 'broader-nutrition'}


def _teacher():
    teacher_root = os.environ.get('TEACHER_ROOT')
    if teacher_root:
        sys.path.insert(0, teacher_root)
    from teacher import coordinator, result_workflow, staged_adapter
    return coordinator, result_workflow, staged_adapter


def target(slice_id):
    return STORE + '/' + hashlib.sha256(slice_id.encode()).hexdigest() + '.json'


def evidence(root, items):
    support(items)
    for ref in items:
        tx.checked(root, ref)
        if not ref.get('quote'):
            raise ValueError('Exact supporting quotation required')


def vocabulary(root, path, scheme):
    from rdflib import Graph, URIRef
    from rdflib.namespace import SKOS
    return {str(s) for s in Graph().parse(root/path).subjects(SKOS.inScheme, URIRef(scheme))}


def validate_review(root, cfg, item, review):
    f = item['findings'][0]; study = f['study_work']
    if f['check'] != CHECK or review.get('slice_id') != item['id']:
        raise ValueError('Exact follow-up assignment required')
    if (review.get('source'), review.get('study_id')) != (study['source'], study['study_id']):
        raise ValueError('Review source identity differs from assignment')
    if review.get('version') != 1 or not review.get('reviewer') or not review.get('reason'):
        raise ValueError('Version, review author and substantive rationale required')
    date(review.get('reviewed_at'))
    evidence(root, review.get('evidence'))
    admitted = json.loads(tx.checked(root, cfg['admissions']))
    if not any(x['study_work']['source'] == study['source'] and x['study_work']['study_id'] == study['study_id'] for x in admitted):
        raise ValueError('Source is not in the explicitly admitted follow-up cohort')
    disposition = review.get('disposition')
    if disposition not in ('publish', 'no-change', 'insufficient-evidence', 'out-of-scope'):
        raise ValueError('Explicit final research disposition required')
    orgs = review.get('organizations', []); project = review.get('project')
    if disposition != 'publish' and (orgs or project):
        raise ValueError('Nonpublication disposition cannot add facts')
    if disposition == 'publish' and not (orgs or project):
        raise ValueError('Publication needs structured supported facts')
    allowed_types = vocabulary(root, 'vocabularies/agent-classification.ttl', TYPE)
    allowed_roles = vocabulary(root, 'vocabularies/agent-classification.ttl', ROLE)
    active = tx.read_head(root, 'evidence/agents')
    existing = json.loads(tx.checked(root, active['outputs']['agents.json'])) if active else {'organizations':[]}
    known = {o['id']: o for o in existing['organizations']}
    if review.get('identity_transaction') != (active['transaction'] if active else None):
        raise ValueError('Refresh review against the active organization identity snapshot')
    for published in publications(root):
        if published['slice_id'] == item['id']: continue
        for o in published['review'].get('organizations', []):
            if o['id'] in known and known[o['id']]['name'] != o['name']: raise ValueError('Conflicting published organization identity')
            known.setdefault(o['id'], o)
    seen = set()
    for o in orgs:
        iri(o['id'])
        if not o['id'].startswith('https://universalevidence.com/agents/') or o['id'] in seen or not o.get('name'):
            raise ValueError('Unique UE organization identity and name required')
        seen.add(o['id']); evidence(root, o.get('identity_evidence'))
        if not o.get('identity_rationale'):
            raise ValueError('Explicit legal/entity identity rationale required')
        if o['id'] in known and o['name'] != known[o['id']]['name']:
            raise ValueError('Renames/merges require the canonical identity revision publisher')
        if o['id'] not in known and not o.get('identifier_reconciliation'):
            raise ValueError('New identities require explicit reconciliation against existing entities')
        for t in o.get('types', []):
            if t.get('uri') not in allowed_types: raise ValueError('Unknown organization type')
            evidence(root, t.get('evidence'))
    if project:
        if project.get('source') != study['source'] or project.get('study_id') != study['study_id']:
            raise ValueError('Project must belong to this assignment')
        from urllib.parse import quote
        expected_id = 'https://universalevidence.com/' + ('projects/' if study['source']=='iati' else 'studies/') + study['source'] + '/' + quote(study['study_id'], safe='')
        if project.get('id') != expected_id: raise ValueError('Project identity must be the deterministic source-qualified URI')
        iri(project['id']); iri(project['source_url'])
        if not project.get('title') or not project.get('status') or not project.get('identity_reconciliation'):
            raise ValueError('Project title, historical/source status and identifier reconciliation required')
        if project['status'] in ('current','operating','verified-current'):
            raise ValueError('Retained evidence cannot assert verified current operation')
        evidence(root, project.get('identity_evidence'))
        if not project.get('relevance'): raise ValueError('Explicit relevance basis required')
        # Some legacy vocabularies omit scheme membership; require an actual SKOS concept.
        from rdflib import Graph, URIRef
        from rdflib.namespace import RDF, SKOS
        sg = Graph().parse(root/'vocabularies/states.ttl'); ig = Graph().parse(root/'vocabularies/interventions.ttl')
        for r in project['relevance']:
            if r.get('basis') not in BASES or r.get('state') != 'https://universalevidence.com/vocab/states/Stunting' or not r.get('reason') or (URIRef(r.get('state','')),RDF.type,SKOS.Concept) not in sg:
                raise ValueError('Reviewed relevance basis and canonical State required')
            evidence(root, r.get('evidence'))
            if not r.get('interventions'): raise ValueError('Intervention attribution required')
            for i in r['interventions']:
                if (URIRef(i.get('uri','')),RDF.type,SKOS.Concept) not in ig or not i.get('label'):
                    raise ValueError('Canonical intervention required')
                if i.get('arm') not in ('intervention','comparator'): raise ValueError('Explicit treatment/comparator arm required')
                evidence(root, i.get('evidence'))
        for r in project.get('roles', []):
            if r.get('organization_id') not in seen or r.get('role') not in allowed_roles:
                raise ValueError('Role requires an explicitly reviewed identity and controlled role')
            if r.get('scope') not in ('project','intervention'):
                raise ValueError('Explicit project or intervention scope required')
            if r['scope'] == 'intervention' and r.get('intervention') not in {i['uri'] for x in project['relevance'] for i in x['interventions']}:
                raise ValueError('Role intervention is outside reviewed project')
            evidence(root, r.get('evidence'))
            if not r.get('rationale'): raise ValueError('Role rationale required; sponsorship does not imply delivery')
    for ref in [*refs(review), *cfg.get('dependencies', [])]: tx.checked(root, ref)
    return review


def prepare(root, cfg, item, review_path):
    review = json.loads(Path(review_path).read_text())
    validate_review(root, cfg, item, review)
    return {'review': tx.reference(root, review_path), 'reviewer': review['reviewer'],
            'summary': review['reason'], 'expected_outputs':[target(item['id'])]}


def validate(root, cfg, item, payload):
    review = json.loads(tx.checked(root, payload['review']))
    if payload['reviewer'] != review['reviewer']: raise ValueError('Review author mismatch')
    validate_review(root, cfg, item, review)


def rdf(review):
    from rdflib import Graph, Namespace, URIRef as U, Literal as L
    from rdflib.namespace import RDF, DCTERMS, SKOS
    g=Graph(); S=Namespace('https://schema.org/'); P=Namespace('http://www.w3.org/ns/prov#'); UE=Namespace('https://universalevidence.com/ontology/')
    for o in review.get('organizations',[]):
        n=U(o['id']);g.add((n,RDF.type,P.Organization));g.add((n,SKOS.prefLabel,L(o['name'])))
        for t in o.get('types',[]):g.add((n,S.additionalType,U(t['uri'])))
        g.add((n,DCTERMS.description,L(json.dumps(o,sort_keys=True))))
    assertion_base='https://universalevidence.com/organization-followups/'+tx.sha(tx.encoded(review))
    p=review.get('project')
    if p:
        n=U(p['id']);g.add((n,RDF.type,S.Project));g.add((n,DCTERMS.title,L(p['title'])));g.add((n,DCTERMS.source,U(p['source_url'])))
        for index,r in enumerate(p['relevance']):
            node=U(assertion_base+'/relevance/'+str(index));g.add((n,S.about,node))
            g.add((node,DCTERMS.type,L(r['basis'])));g.add((node,S.about,U(r['state'])));g.add((node,DCTERMS.description,L(json.dumps(r,sort_keys=True))))
            if r['basis']=='explicit-stunting-objective':g.add((n,UE.intendedState,U(r['state'])))
        for index,r in enumerate(p.get('roles',[])):
            node=U(assertion_base+'/role/'+str(index));g.add((node,RDF.type,S.Role));g.add((node,S.about,n));g.add((node,S.member,U(r['organization_id'])));g.add((node,S.additionalType,U(r['role'])));g.add((node,DCTERMS.description,L(json.dumps(r,sort_keys=True))))
    return ''.join(sorted(g.serialize(format='nt').splitlines(keepends=True)))


def render(root, cfg, item, payload):
    from scripts import organization_followups as lifecycle
    if not lifecycle._executing: raise ValueError('Use organization_followups execute to hold the shared publication lock')
    return render_record(root,cfg,item,payload)


def render_record(root,cfg,item,payload):
    review=json.loads(tx.checked(root,payload['review']))
    record={'version':1,'slice_id':item['id'],'review':review,'rdf':rdf(review)}
    summary={'resolved':1,'added':1 if review['disposition']=='publish' else 0,'disposition':review['disposition']}
    return target(item['id']),tx.encoded(record).decode(),summary


def verify(root,cfg,item,payload,body):
    validate(root,cfg,item,payload)
    _,expected,_=render_record(root,cfg,item,payload)
    if body!=expected.encode():raise ValueError('Published record differs from reviewed output')


def handoff(queue, config, review_path, agent, output):
    c,wf,adapter=_teacher();data,lock=c._load_queue_locked(Path(queue))
    try:
        root=Path(data['project_root']);cfgref=tx.reference(root,config);cfg=json.loads(tx.checked(root,cfgref));review=json.loads(Path(review_path).read_text())
        if (root/cfg['queue']).resolve()!=Path(queue).resolve():raise ValueError('Queue mismatch')
        item=next(s for s in data['slices'] if s['id']==review['slice_id'])
        if item['status']!='claimed' or item.get('claimed_by')!=agent or item['result_workflow']['stage']!='hygiene':raise ValueError('Owned Hygiene claim required')
        if review['reviewer']!=agent:raise ValueError('Review author must be actual Hygiene worker')
        payload=prepare(root,cfg,item,review_path);payload.update(slice_id=item['id'],config=cfgref,evidence=[tx.reference(root,review_path),*list(refs(review['evidence']))])
        adapter.validate(root,item,payload)
        path=tx.inside(root,output)/('operation-'+wf.digest(payload)+'.json');tx.immutable(path,tx.encoded(payload))
        packet={'slice_id':item['id'],'author':agent,'summary':payload['summary'],'evidence':[tx.reference(root,path),cfgref,cfg['adapter'],*payload['evidence']], 'operations':[{'kind':'adapter-submission','payload':tx.reference(root,path)}],'expected_outputs':payload['expected_outputs']}
        wf.transition(root,item,agent,'handoff',packet,c._now());c._save_queue(data,lock,operation='organization-followup-handoff',details={'slice':item['id'],'agent':agent})
        return {'slice':item['id'],'operation':str(path),'stage':'execute'}
    finally:lock.close()


def execute(queue, slice_id, agent, payload_path, dry_run=False):
    """Serialize cross-queue identity validation and commit under one shared lock."""
    global _executing
    _,_,adapter = _teacher()
    root = Path(json.loads(Path(queue).read_text())['project_root'])
    with tx.locked(root/STORE):
        _executing = True
        try: return adapter.execute(Path(queue),slice_id,agent,Path(payload_path),dry_run)
        finally: _executing = False


def reopen(queue, slice_ids, approval_path, worker):
    """Explicit engineering recovery; preserve blocker evidence and queue history."""
    c,wf,_ = _teacher()
    data,lock=c._load_queue_locked(Path(queue))
    try:
        root=Path(data['project_root']);approval=json.loads(Path(approval_path).read_text())
        if not approval.get('approved') or not approval.get('reviewer') or approval['reviewer']==worker:
            raise ValueError('Independent engineering review required')
        for ref in approval['evidence']: tx.checked(root,ref)
        rows=[next(s for s in data['slices'] if s['id']==sid) for sid in slice_ids]
        for item in rows:
            if item['findings'][0]['check']!=CHECK or item['status']!='blocked' or item.get('result_workflow',{}).get('stage')!='hygiene' or item.get('commit_receipt') or item.get('commit_intent') or item.get('blocker',{}).get('review_kind')!='engineering':
                raise ValueError('Only uncommitted engineering-blocked follow-up Hygiene can reopen')
        proof=tx.reference(root,approval_path)
        for item in rows:
            previous=copy.deepcopy(item['blocker'])
            item.setdefault('blocker_history',[]).append({**previous,'resumed_at':c._now(),'resumed_by':worker,'previous_owner':item.get('claimed_by'),'resolution':'Independently reviewed supplemental publication route available. Outstanding research remains Hygiene work.','engineering_review':proof})
            item.pop('blocker');item['status']='pending'
            for field in ('claimed_by','claimed_at','claimed_capabilities'):item.pop(field,None)
        c._save_queue(data,lock,operation='organization-followup-engineering-reopen',details={'slices':slice_ids,'agent':worker,'engineering_review':proof})
        return {'reopened':len(rows),'stage':'hygiene'}
    finally:lock.close()


def publications(root):
    """Only durable independent Execute receipts activate immutable records."""
    root=Path(root);log=root/'data/taxonomy-expansion/hygiene-commits.jsonl'
    if not log.exists():return []
    receipts={}
    for line in log.read_text().splitlines():
        if line.strip():
            r=json.loads(line);receipts.setdefault(r['slice_id'],[]).append(r)
    records=[]
    for path in sorted((root/STORE).glob('*.json')):
        record=json.loads(path.read_text());sid=record['slice_id'];journal=root/'data/taxonomy-expansion/adapter-transactions'/(sid+'.json')
        if not journal.exists():continue
        t=json.loads(journal.read_text());rows=receipts.get(sid,[])
        if len(rows)!=1 or rows[0]!=t['receipt'] or tx.sha(path.read_bytes())!=t['output_sha256']:continue
        if t.get('status') != 'committed' or rows[0]['agent']==record['review']['reviewer']:continue
        for ref in refs(record['review']): tx.checked(root, ref)
        if Path(t['target']).as_posix()!=str(path.relative_to(root)):continue
        if record['rdf']!=rdf(record['review']):raise ValueError('RDF/JSON publication mismatch')
        records.append(record)
    return records


def cards(root,state,intervention,include_broader=False):
    result=[]
    for record in publications(root):
        review=record['review'];p=review.get('project')
        if not p:continue
        contexts=[r for r in p['relevance'] if r['state']==state and (include_broader or r['basis']!='broader-nutrition') and any(i['uri']==intervention and i['arm']=='intervention' for i in r['interventions'])]
        if not contexts:continue
        orgs={o['id']:o for o in review['organizations']};roles=[]
        for r in p.get('roles',[]):
            if r['scope']=='intervention' and r['intervention']!=intervention:continue
            o=orgs[r['organization_id']];role=r['role'].rsplit('/',1)[-1]
            roles.append({'name':o['name'],'organization_id':o['id'],'organization_types':o.get('types',[]),'identity_status':'verified','role':'implementer' if role=='Implementer' else role,'scope':{'kind':'context' if r['scope']=='project' else 'component','id':p['id']},'evidence':r['evidence']})
        implementers=[r for r in roles if r['role']=='implementer']
        result.append({'id':p['id'],'title':p['title'],'source':p['source'],'study_id':p['study_id'],'source_url':p['source_url'],'reviewed_at':review['reviewed_at'],'geography':p.get('geography',[]),'dates':p.get('dates',[]),'status':p['status'],'aliases':[],'provenance':'reviewed-organization-followup','relevance_bases':sorted({r['basis'] for r in contexts}),'match_reasons':[r['reason'] for r in contexts],'contexts':[{'id':p['id'],'components':r['interventions']} for r in contexts],'implementers':implementers,'other_roles':[r for r in roles if r not in implementers],'gaps':p.get('gaps',[]),'missing_role':None if implementers else 'Implementing organization not yet verified'})
    return result


def merge_cards(root, existing, state, intervention):
    """Add reviewed projects/roles without deleting earlier verified assertions."""
    result = copy.deepcopy(existing)
    by_key = {(p['source'],p['study_id']):p for p in result}
    for p in cards(root,state,intervention):
        key=(p['source'],p['study_id'])
        if key not in by_key:
            result.append(p);by_key[key]=p;continue
        old=by_key[key]
        for field in ('implementers','other_roles','contexts','match_reasons','gaps'):
            for value in p.get(field,[]):
                if value not in old.setdefault(field,[]): old[field].append(value)
        old['relevance_bases']=sorted(set(old.get('relevance_bases',[])+p['relevance_bases']))
        if old['implementers']: old['missing_role']=None
    types={}
    for record in publications(root):
        for org in record['review'].get('organizations',[]):
            types.setdefault(org['id'],[])
            for t in org.get('types',[]):
                if t not in types[org['id']]:types[org['id']].append(t)
    for p in result:
        for role in p['implementers']+p['other_roles']:
            if role.get('organization_id') in types:role['organization_types']=types[role['organization_id']]
    return result


def main():
    p=argparse.ArgumentParser(description=__doc__);sub=p.add_subparsers(dest='command',required=True)
    h=sub.add_parser('prepare');h.add_argument('--queue',type=Path,required=True);h.add_argument('--config',type=Path,required=True);h.add_argument('--review',type=Path,required=True);h.add_argument('--agent',required=True);h.add_argument('--output',type=Path,required=True)
    e=sub.add_parser('execute');e.add_argument('--queue',type=Path,required=True);e.add_argument('--slice',required=True);e.add_argument('--agent',required=True);e.add_argument('--payload',type=Path,required=True);e.add_argument('--dry-run',action='store_true')
    e=sub.add_parser('export');e.add_argument('--root',type=Path,default=ROOT);e.add_argument('--output',type=Path,required=True)
    a=p.parse_args()
    if a.command=='prepare':print(json.dumps(handoff(a.queue,a.config,a.review,a.agent,a.output),indent=2))
    elif a.command=='execute':
        # CLI runs as __main__; keep the adapter's shared guard on the importable module.
        from scripts.organization_followups import execute as run
        print(json.dumps(run(a.queue,a.slice,a.agent,a.payload,a.dry_run),indent=2))
    else:
        records=publications(a.root);a.output.mkdir(parents=True,exist_ok=True)
        tx.atomic(a.output/'organizations.json',tx.encoded(records));tx.atomic(a.output/'agents-followups.ttl',''.join(r['rdf'] for r in records).encode());print(json.dumps({'publications':len(records)}))

if __name__=='__main__':main()
