"""Bounded IATI activity metadata and reviewed mappings, isolated from research data.

Turtle is authoritative. Writers use locked, recoverable file transactions; API readers
use a read-only RDF snapshot. No inferred roles, lexical auto-acceptance or name merges.
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
import re
try:
    from scripts import rdf_store
except ModuleNotFoundError:
    import rdf_store
StoreError = rdf_store.StoreError
from urllib.parse import quote
from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import RDF, RDFS, SKOS, DCTERMS, XSD

ROOT=Path(__file__).resolve().parents[1]
VOCABULARY_ROOT=Path(os.getenv('UE_VOCABULARY_ROOT',ROOT/'vocabularies'))
UE=Namespace('https://universalevidence.com/ontology/')
PROV=Namespace('http://www.w3.org/ns/prov#')
AGENTS='https://universalevidence.com/vocab/agents/'
ACTIVITIES='https://universalevidence.com/activities/iati/'
ROLES={'1':'funding','2':'accountable','3':'extending','4':'implementing'}
MAX_ACTIVITIES=1000

class Conflict(ValueError): pass

def canonical(value):return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False)
def digest(value):return hashlib.sha256(canonical(value).encode()).hexdigest()
def now():return datetime.now(timezone.utc).isoformat()
def date(value):
    parsed=datetime.fromisoformat(value.replace('Z','+00:00'))
    if parsed.tzinfo is None:raise ValueError('Timestamp requires timezone')
    return parsed

SCHEMA='iati-actors-ttl-1'

def initialize(conn):
    if conn.schema!=SCHEMA:raise ValueError('Unsupported actor store schema')
    conn.workflow.setdefault('actor_operations',{});conn.workflow.setdefault('actor_discovery',{})

def writer(path):
    @contextmanager
    def run():
        with rdf_store.transaction(path,SCHEMA,export_activities) as conn:
            initialize(conn);yield conn
    return run()

@contextmanager
def reader(path=None):
    path=path or os.getenv('UE_ACTORS_TTL')
    if not path:raise FileNotFoundError('Actor store is not configured')
    with rdf_store.snapshot(path,SCHEMA,export_activities) as conn:yield conn

def open_store(path):return rdf_store.Store(path,SCHEMA,export_activities)
def get(conn,identity):return deepcopy(conn.data['activities'].get(identity))
def generation(conn):return digest([conn.meta['generation'],conn.revision])
def bump(conn):
    conn.meta['generation']=str(int(conn.meta['generation'])+1);conn.meta['updated_at']=now()
def narratives(values):return [str(n['text()']) for v in values for n in v.get('narrative',[]) if n.get('text()')]
def usable_ref(ref):
    placeholders={'unknown','n/a','na','none','null','not-known','not-published','withheld','undisclosed'}
    return isinstance(ref,str) and ref.casefold() not in placeholders and bool(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:/-]*',ref))

def withheld_name(name):
    normalized=' '.join(name.casefold().split()).strip(' .')
    return normalized in {'ip not published','implementing partner not published','not published','withheld','name withheld','identity withheld','not disclosed','undisclosed','confidential','redacted'}
def org_uri(ref):return AGENTS+'iati-'+digest(ref)

def normalize(doc,retrieved_at):
    date(retrieved_at)
    nested=doc.get('iati_json')
    if isinstance(nested,str):nested=json.loads(nested)
    activities=(nested or {}).get('iati-activity',[])
    if len(activities)!=1:raise ValueError('One complete nested iati_json activity required')
    raw=activities[0];ids=raw.get('iati-identifier',[])
    if len(ids)!=1 or not ids[0].get('text()'):raise ValueError('One activity identifier required')
    identity=ids[0]['text()'];publisher=deepcopy(raw.get('reporting-org',[]))
    fields={'titles':deepcopy(raw.get('title',[])),'descriptions':deepcopy(raw.get('description',[])),
            'publisher':publisher,'status':deepcopy(raw.get('activity-status',[])),
            'dates':deepcopy(raw.get('activity-date',[])),
            'geography':{k:deepcopy(raw[k]) for k in ('recipient-country','recipient-region','location') if k in raw},
            'sectors':[],'participants':[],'source_updated_at':raw.get('@last-updated-datetime'),
            'source_version':raw.get('@version'),'language':raw.get('@xml:lang')}
    sector_entries=[('activity',None,s) for s in raw.get('sector',[])]
    for i,transaction in enumerate(raw.get('transaction',[])):
        sector_entries.extend(('transaction',{'index':i,'ref':transaction.get('@ref')},s) for s in transaction.get('sector',[]))
    for level,transaction,sector in sector_entries:
        # Bind whole nested entry, including publisher scope, never zip flattened arrays.
        fields['sectors'].append({'published':deepcopy(sector),'publisher':publisher,'level':level,'transaction':transaction,
            'vocabulary':sector.get('@vocabulary','1'),'vocabulary_uri':sector.get('@vocabulary-uri'),
            'version':sector.get('@version'),'publisher_scoped':str(sector.get('@vocabulary','1')) in {'98','99'}})
    unique={}
    for entry in raw.get('participating-org',[]):
        ref=entry.get('@ref') or None;names=narratives([entry]);role=str(entry.get('@role',''))
        withheld=any(withheld_name(n) for n in names)
        status='withheld' if withheld else 'identified' if usable_ref(ref) else 'unresolved'
        key=digest([identity,publisher,entry])
        unique[key]={'id':key,'published':deepcopy(entry),'ref':ref,'names':names,
          'type':entry.get('@type'),'role':role,'role_label':ROLES.get(role,'unknown'),
          'identity_status':status,'organization':org_uri(ref) if status=='identified' else None,
          'publisher':publisher}
    fields['participants']=[unique[k] for k in sorted(unique)]
    missing=[k for k in ('titles','publisher','status','dates','geography') if not fields[k]]
    by_type={}
    for entry in fields['dates']:by_type.setdefault(entry.get('@type','unspecified'),set()).add(entry.get('@iso-date'))
    fields['metadata_issues']={'missing':missing,'conflicting_status':len({x.get('@code') for x in fields['status']})>1,'conflicting_date_types':sorted(k for k,v in by_type.items() if len(v)>1),'current_operation':'not inferred'}
    if len(canonical(fields))>2_000_000:raise ValueError('Activity metadata exceeds 2MB cap')
    return {'id':identity,'uri':ACTIVITIES+quote(identity,safe=''),'source_url':'https://d-portal.org/q.html?aid='+quote(identity,safe=''),
            'source':fields,'fingerprint':digest(fields),'retrieved_at':retrieved_at}

@lru_cache(maxsize=8)
def taxonomy(axis,mtime):return Graph().parse(VOCABULARY_ROOT/('states.ttl' if axis=='state' else 'interventions.ttl'))
def concept(uri,axis):
    if axis not in {'state','intervention'}:raise ValueError('Unknown mapping axis')
    path=VOCABULARY_ROOT/('states.ttl' if axis=='state' else 'interventions.ttl')
    graph=taxonomy(axis,path.stat().st_mtime_ns)
    if (URIRef(uri),RDF.type,UE.State if axis=='state' else UE.Intervention) not in graph:raise ValueError('Expected canonical '+axis+' URI')
    return graph

def locate(source,path):
    if not isinstance(path,list) or not path or path[0] not in {'titles','descriptions','sectors'}:raise ValueError('Support must cite title, description or sector')
    if path[0]=='sectors' and len(path)!=2:raise ValueError('Cite whole sector entry including vocabulary and publisher scope')
    try:
        value=source
        for key in path:value=value[key]
    except (KeyError,IndexError,TypeError):raise ValueError('Missing support field') from None
    return value

def supported(source,mapping):
    try:return bool(mapping.get('support')) and all(locate(source,s['path'])==s['value'] for s in mapping['support'])
    except ValueError:return False

def validate_decision(source,decision):
    for key in ('axis','concept','status','method','reviewer','reviewed_at','rationale'):
        if not decision.get(key):raise ValueError('Mapping requires '+key)
    concept(decision['concept'],decision['axis']);date(decision['reviewed_at'])
    if decision['status'] not in {'accepted','rejected','ambiguous'}:raise ValueError('Unknown mapping decision')
    if decision['method'] not in {'source-text-review','sector-code-review'}:raise ValueError('Search hits are not mapping decisions')
    if decision['status']=='accepted':
        if not supported(source,decision):raise ValueError('Accepted mapping has missing or changed support')
        if any(s['value'] in (None,'',[],{}) for s in decision['support']):raise ValueError('Empty support')
        if decision['method']=='sector-code-review':
            review=decision.get('crosswalk_review') or {}
            if not all(review.get(k) for k in ('source_url','reviewer','reviewed_at','scope_rationale')) or review.get('concept')!=decision['concept'] or not all(s['path'][0]=='sectors' for s in decision['support']):raise ValueError('Code mapping needs reviewed scoped crosswalk provenance')
            date(review['reviewed_at'])
    return {**deepcopy(decision),'support_fingerprint':digest(decision.get('support',[]))}

def update(conn,identity,expected,*,document=None,retrieved_at=None,complete=False,decisions=None,resolutions=None,error=None,withdrawal=None):
    """Atomic compare-and-swap. Exact operation retries return their original revision."""
    event={'expected':expected,'document':document,'retrieved_at':retrieved_at,'complete':complete,'decisions':decisions,'resolutions':resolutions,'error':error,'withdrawal':withdrawal}
    op=digest([identity,event]);prior=conn.workflow['actor_operations'].get(op)
    if prior:return prior['revision']
    old=get(conn,identity)
    if (old['revision'] if old else None)!=expected:raise Conflict('Stale activity revision; reread and reconcile')
    if old is None and len(conn.data['activities'])>=MAX_ACTIVITIES:raise ValueError('Bounded actor store is full')
    if old is None and (document is None or not complete):raise ValueError('New activity requires complete source metadata')
    item=deepcopy(old) if old else {'id':identity,'mappings':[],'resolutions':{},'withdrawn':False,'freshness_errors':[]}
    item.pop('revision',None)
    if document is not None:
        if not complete:
            item['freshness_errors'].append({'at':retrieved_at or now(),'reason':'incomplete-activity-refresh'})
        else:
            fresh=normalize(document,retrieved_at)
            if fresh['id']!=identity:raise ValueError('Activity identifier changed')
            relation='first-seen' if not old else 'unchanged' if old['fingerprint']==fresh['fingerprint'] else 'changed-without-publisher-ordering-evidence'
            if old:
                before=old['source'].get('source_updated_at');after=fresh['source'].get('source_updated_at')
                if before and after:
                    try:
                        if date(after)<date(before):raise Conflict('Older source revision')
                        if date(after)>date(before):relation='publisher-timestamp-advanced'
                    except Conflict:raise
                    except (ValueError,TypeError):pass  # Preserve malformed published dates explicitly.
            item.update(fresh);item['source_revision_relation']=relation;item['freshness_errors']=[]
            for mapping in item['mappings']:
                if mapping['status']=='accepted' and not supported(item['source'],mapping):mapping['status']='needs-review'
            # Decisions resolving a disappeared participant remain in history only.
            current={p['id'] for p in item['source']['participants']}
            item['resolutions']={k:v for k,v in item['resolutions'].items() if k in current}
    if error:
        # Callers pass a bounded error classification, never raw credential-bearing responses.
        if error not in {'http-error','network-error','invalid-response','incomplete-response'}:raise ValueError('Unknown refresh error classification')
        item['freshness_errors'].append({'at':retrieved_at or now(),'reason':error})
    if withdrawal is not None:
        if not all(withdrawal.get(k) for k in ('reviewer','reviewed_at','rationale','source_url')):raise ValueError('Explicit withdrawal review required')
        date(withdrawal['reviewed_at']);item['withdrawn']=True;item['withdrawal']=withdrawal
    for decision in decisions or []:
        checked=validate_decision(item['source'],decision)
        key=(checked['axis'],checked['concept'])
        item['mappings']=[m for m in item['mappings'] if (m['axis'],m['concept'])!=key]+[checked]
    for resolution in resolutions or []:
        participant=next((p for p in item['source']['participants'] if p['id']==resolution.get('participant')),None)
        if not participant or participant['identity_status']!='unresolved':raise ValueError('Only unresolved non-withheld participants may be resolved')
        if not usable_ref(resolution.get('ref')) or not all(resolution.get(k) for k in ('reviewer','reviewed_at','rationale','source_url')):raise ValueError('Identity resolution needs a usable ref and review evidence')
        date(resolution['reviewed_at']);item['resolutions'][participant['id']]=deepcopy(resolution)
    item['mappings'].sort(key=lambda m:(m['axis'],m['concept']))
    item['freshness_errors']=item['freshness_errors'][-20:]
    if len(item['mappings'])>100 or len(canonical(item))>4_000_000:raise ValueError('Activity review metadata cap exceeded')
    revision=digest([expected,op,item])
    conn.data['activities'][identity]={**deepcopy(item),'revision':revision}
    event_id=digest([identity,revision])
    conn.data['events'][event_id]={'id':event_id,'activity':identity,'revision':revision,'snapshot':deepcopy(item),'event':{k:v for k,v in event.items() if k!='document'},'recorded_at':now(),'sequence':len(conn.data['events'])+1}
    conn.workflow['actor_operations'][op]={'activity':identity,'revision':revision};bump(conn)
    return revision


def save_population(conn,manifest):
    conn.workflow.setdefault('actor_discovery',{})[manifest['id']]=deepcopy(manifest)
    published=deepcopy(manifest);published.pop('in_flight',None)
    for error in published.get('errors',[]):error.pop('token',None)
    if conn.data['populations'].get(manifest['id'])==published:return
    conn.data['populations'][manifest['id']]=published;bump(conn)


def detail(conn,identity,history=False):
    item=get(conn,identity)
    if not item:raise KeyError(identity)
    item['historical_status_note']='Published status and dates; current operation is not inferred from retrieval/update time.'
    if history:item['history']=[{k:v for k,v in e.items() if k not in ('id','activity')} for e in sorted(conn.data['events'].values(),key=lambda e:(e.get('sequence',0),e.get('recorded_at',''))) if e.get('activity')==identity]
    return item


def eligible_activities(conn):
    """Current activities supported by an accepted State in their recorded population."""
    manifests=[deepcopy(m) for _,m in sorted(conn.data['populations'].items())]
    scope_by_activity={}
    for manifest in manifests:
        for identity in manifest.get('activity_ids',[]):scope_by_activity.setdefault(identity,set()).add(manifest['state'])
    for identity in sorted(conn.data['activities']):
        item=get(conn,identity)
        accepted={(m['axis'],m['concept']) for m in item['mappings'] if m['status']=='accepted'}
        if not item['withdrawn'] and any(('state',scope) in accepted for scope in scope_by_activity.get(identity,set())):
            yield item

def lookup(conn,*,state=None,intervention=None,role=None,limit=25,offset=0,snapshot=None,unresolved=False):
    if not state and not intervention:raise ValueError('State or Intervention URI required')
    if state:concept(state,'state')
    if intervention:concept(intervention,'intervention')
    if role is not None and role not in ROLES:raise ValueError('Role must be an IATI participation role 1–4')
    if not 1<=limit<=100 or offset<0:raise ValueError('Invalid page bounds')
    version=generation(conn)
    token=digest([version,state,intervention,role,unresolved])
    if (offset and snapshot!=token) or (snapshot is not None and snapshot!=token):raise Conflict('Population or query changed; restart pagination')
    manifests=[deepcopy(m) for _,m in sorted(conn.data['populations'].items())]
    organizations={};unknown={};matching=[]
    for item in eligible_activities(conn):
        identity=item['id']
        accepted={(m['axis'],m['concept']) for m in item['mappings'] if m['status']=='accepted'}
        if state and ('state',state) not in accepted:continue
        if intervention and ('intervention',intervention) not in accepted:continue
        matching.append(identity)
        evidence={'id':identity,'title':narratives(item['source']['titles']),'source_url':item['source_url'],
          'publisher':item['source']['publisher'],'mapped_concepts':[m for m in item['mappings'] if m['status']=='accepted'],
          'status':item['source']['status'],'dates':item['source']['dates'],'geography':item['source']['geography'],'metadata_issues':item['source']['metadata_issues'],
          'retrieved_at':item['retrieved_at'],'source_updated_at':item['source']['source_updated_at'],
          'revision':item['revision'],'freshness_errors':item['freshness_errors'],'source_revision_relation':item.get('source_revision_relation'),
          'attribution':'Published participation in this mapped activity; no organization-specific intervention delivery or effectiveness claim.'}
        for participant in item['source']['participants']:
            if role is not None and participant['role']!=role:continue
            p=deepcopy(participant);resolution=item['resolutions'].get(p['id'])
            if resolution:p.update(organization=org_uri(resolution['ref']),identity_status='reviewed-resolution',identity_resolution=resolution)
            if not p['organization']:
                unknown[p['id']]={**p,'activity':evidence};continue
            org=organizations.setdefault(p['organization'],{'id':p['organization'],'identity_status':'identified','refs':set(),'names':set(),'types':set(),'activities':{}})
            org['refs'].add(resolution['ref'] if resolution else p['ref']);org['names'].update(p['names'])
            if p['type']:org['types'].add(p['type'])
            support=org['activities'].setdefault(identity,{**evidence,'roles':{},'participants':[]})
            support['roles'][p['role']]=p['role_label'];support['participants'].append(p)
    for org in organizations.values():
        for key in ('refs','names','types'):org[key]=sorted(org[key])
        org['name_unavailable']=not org['names'];org['aliases']=org['names'];org['activities']=list(org['activities'].values())
    selected=unknown if unresolved else organizations;keys=sorted(selected)
    return {'results':[selected[k] for k in keys[offset:offset+limit]],'meta':{
      'identified_organization_count':len(organizations),'unresolved_participant_count':len(unknown),
      'unresolved_count_unit':'distinct source participant records, not real-world organizations',
      'matching_activity_count':len(matching),'offset':offset,'limit':limit,'snapshot':token,
      'next_offset':offset+limit if offset+limit<len(keys) else None,'population_scope':manifests,
      'exhaustive':False,'population_timestamp':conn.meta.get('updated_at'),
      'freshness':'last-known reviewed mappings; inspect supporting activity freshness_errors',
      'concept_matching':'exact canonical URI; no implicit descendant expansion'}}


def export_agents(conn):
    graph=Graph().parse(ROOT/'vocabularies/agents.ttl')
    # Only independently eligible current support contributes identities, labels and sources.
    for item in eligible_activities(conn):
        for p in item['source']['participants']:
            resolution=item['resolutions'].get(p['id'])
            uri=org_uri(resolution['ref']) if resolution else p['organization']
            if not uri:continue
            node=URIRef(uri);graph.add((node,RDF.type,PROV.Organization));graph.add((node,DCTERMS.isPartOf,URIRef(AGENTS)))
            graph.add((node,DCTERMS.identifier,Literal(resolution['ref'] if resolution else p['ref'])))
            graph.add((node,DCTERMS.source,URIRef(item['source_url'])))
            for name in p['names']:graph.add((node,RDFS.label,Literal(name)));graph.add((node,SKOS.altLabel,Literal(name)))
    return graph


def export_activities(conn):
    """PROV qualified associations express published activity roles, not delivery."""
    graph=export_agents(conn)
    graph.bind('prov',PROV);graph.bind('dcterms',DCTERMS)
    for item in eligible_activities(conn):
        identity=item['id'];activity=URIRef(item['uri'])
        graph.add((activity,RDF.type,PROV.Activity));graph.add((activity,DCTERMS.identifier,Literal(identity)))
        graph.add((activity,DCTERMS.source,URIRef(item['source_url'])))
        for title in narratives(item['source']['titles']):graph.add((activity,DCTERMS.title,Literal(title)))
        snapshot=URIRef(str(activity)+'/revisions/'+item['revision'])
        graph.add((snapshot,DCTERMS.references,activity))
        graph.add((snapshot,RDF.type,PROV.Entity));graph.add((snapshot,PROV.wasDerivedFrom,URIRef(item['source_url'])))
        graph.add((snapshot,PROV.generatedAtTime,Literal(item['retrieved_at'],datatype=XSD.dateTime)))
        if item['withdrawn']:continue
        for p in item['source']['participants']:
            resolution=item['resolutions'].get(p['id']);org=org_uri(resolution['ref']) if resolution else p['organization']
            if not org or p['role'] not in ROLES:continue
            association=URIRef(str(snapshot)+'/associations/'+p['id'])
            graph.add((activity,PROV.qualifiedAssociation,association));graph.add((association,RDF.type,PROV.Association))
            graph.add((association,PROV.agent,URIRef(org)))
            role=URIRef(ACTIVITIES+'roles/'+p['role'])
            graph.add((role,RDF.type,PROV.Role));graph.add((role,DCTERMS.identifier,Literal(p['role'])))
            graph.add((role,RDFS.label,Literal(ROLES[p['role']])))
            graph.add((role,DCTERMS.source,URIRef('https://iatistandard.org/en/iati-standard/203/codelists/organisationrole/')))
            graph.add((association,PROV.hadRole,role))
            graph.add((association,DCTERMS.source,snapshot))
        for m in item['mappings']:
            if m['status']!='accepted':continue
            target=URIRef(m['concept']);graph.add((activity,DCTERMS.subject,target))
            assertion=URIRef(str(snapshot)+'/mappings/'+digest([m['axis'],m['concept']]))
            graph.add((assertion,RDF.type,PROV.Entity))
            for predicate,value in ((RDF.type,RDF.Statement),(RDF.subject,activity),(RDF.predicate,DCTERMS.subject),(RDF.object,target),(PROV.wasDerivedFrom,snapshot),(DCTERMS.description,Literal(canonical(m)))):graph.add((assertion,predicate,value))
    return graph


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('command',choices=['init','detail','export-agents','export-activities','lookup','backup'])
    p.add_argument('--store',required=True);p.add_argument('--id');p.add_argument('--state');p.add_argument('--intervention');p.add_argument('--role');p.add_argument('--output')
    a=p.parse_args(argv)
    if a.command=='init':
        with writer(a.store):pass
        return
    with reader(a.store) as conn:
        if a.command=='export-agents':export_agents(conn).serialize(a.output,format='turtle');return
        if a.command=='export-activities':export_activities(conn).serialize(a.output,format='turtle');return
        if a.command=='backup':
            if Path(a.output).exists():raise ValueError('Backup destination must be new')
            rdf_store.backup(a.store,a.output,SCHEMA,export_activities);return
        result=detail(conn,a.id,True) if a.command=='detail' else lookup(conn,state=a.state,intervention=a.intervention,role=a.role)
        print(json.dumps(result,indent=2,ensure_ascii=False))


# Project-owned source adapter functions used by the reusable Teacher population lane.
def quoted(term):
    return '"'+re.sub(r'([+\-!(){}\[\]^"~*?:\\/|&])',r'\\\1',term)+'"'

def make_plan(state,interventions=(),activity_cap=25,query_cap=10,request_cap=20):
    if not 1<=activity_cap<=MAX_ACTIVITIES or not 1<=query_cap<=50 or not 1<=request_cap<=100:raise ValueError('Invalid discovery caps')
    def labels(uri,axis):
        graph=concept(uri,axis)
        return sorted({str(v) for p in (SKOS.prefLabel,SKOS.altLabel) for v in graph.objects(URIRef(uri),p) if not v.language or v.language.lower().startswith('en')})
    state_labels=labels(state,'state')
    if not state_labels:raise ValueError('State has no query labels')
    clauses=[f'(title_narrative:{quoted(t)} OR description_narrative:{quoted(t)})' for t in state_labels]
    state_clause='('+' OR '.join(clauses)+')'
    candidates=[{'query':c,'state_terms':[t],'intervention_terms':[]} for t,c in zip(state_labels,clauses)]
    for uri in sorted(set(interventions)):
        for term in labels(uri,'intervention'):
            candidates.append({'query':state_clause+' AND '+f'(title_narrative:{quoted(term)} OR description_narrative:{quoted(term)} OR sector_narrative:{quoted(term)})','state_terms':state_labels,'intervention_terms':[term],'intervention':uri})
    plan={'schema':'activity-population-1','source':'iati','state':state,'interventions':list(interventions),
      'activity_cap':activity_cap,'query_cap':query_cap,'request_cap':request_cap,'queries':candidates[:query_cap],
      'omitted_query_count':max(0,len(candidates)-query_cap),'scope':'Only source-reviewed activities mapped to the pilot State qualify; Intervention-only answers describe this bounded State population.'}
    plan['id']=digest(plan);return plan

def fetch_page(query,start,rows):
    """Called only by an explicitly requested live command; credentials never persisted."""
    from urllib.request import Request,urlopen
    from urllib.parse import urlencode
    from urllib.error import HTTPError,URLError
    key=os.environ.get('IATI_API_KEY')
    if not key:raise ValueError('IATI_API_KEY environment variable required')
    url='https://api.iatistandard.org/datastore/activity/select?'+urlencode({'q':query,'start':start,'rows':rows,'fl':'iati_identifier,iati_json','sort':'iati_identifier asc','wt':'json'})
    request=Request(url,headers={'Ocp-Apim-Subscription-Key':key,'Accept':'application/json'})
    try:
        with urlopen(request,timeout=30) as response:
            body=response.read(20_000_001)
            if len(body)>20_000_000:raise ValueError('invalid-response')
            return json.loads(body)
    except HTTPError:raise ValueError('http-error') from None
    except URLError:raise ValueError('network-error') from None
    except (TimeoutError,json.JSONDecodeError):raise ValueError('invalid-response') from None


def page(response):
    header=response.get('responseHeader',{})
    result=response.get('response')
    if header.get('partialResults') in (True,'true') or header.get('status',0)!=0:raise ValueError('incomplete-response')
    if not isinstance(result,dict) or not isinstance(result.get('docs'),list) or not isinstance(result.get('numFound'),int):raise ValueError('invalid-response')
    if result.get('numFoundExact') is False:raise ValueError('incomplete-response')
    return result['docs'],result['numFound']

if __name__=='__main__':main()
