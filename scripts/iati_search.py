"""Deterministic source-registry + RDF crosswalk discovery. No delivery inference."""
from __future__ import annotations
import asyncio
from copy import deepcopy
import hashlib
import json
import re
import sqlite3
from pathlib import Path
from rdflib import Graph, Namespace, URIRef, Literal, BNode
from rdflib.namespace import RDF, RDFS, DCTERMS, XSD
from scripts import iati_actors as actors
from scripts.iati_live import Organizations, Quota, SourceError, fetch_live, unpack, project, MAX_RESPONSE_BYTES, ENDPOINT
from scripts.iati_live_rules import activity, contains, quote

ROOT=Path(__file__).resolve().parents[1]
IQ=Namespace('https://universalevidence.com/source/iati/config#')
UE=actors.UE
SOURCE=URIRef('https://universalevidence.com/vocab/sources/iati')
AXES={'state':UE.ConditionAxis,'intervention':UE.InterventionAxis}

class SearchCrosswalk:
    def __init__(self, sources=None, crosswalk=None):
        sources=Path(sources or ROOT/'vocabularies/sources.ttl')
        registry=Graph().parse(sources)
        def one(g,s,p):
            values=list(g.objects(s,p))
            if len(values)!=1:raise ValueError('Expected one source/crosswalk value: '+str(p))
            return str(values[0])
        service=URIRef(one(registry,SOURCE,URIRef('http://www.w3.org/ns/dcat#accessService')))
        endpoint=one(registry,service,URIRef('http://www.w3.org/ns/dcat#endpointURL'))
        if endpoint!=ENDPOINT:raise ValueError('Unsupported IATI transport endpoint')
        relative=one(registry,SOURCE,IQ.searchCrosswalk)
        default=(ROOT/relative).resolve()
        if not default.is_relative_to((ROOT/'vocabularies').resolve()):raise ValueError('Invalid crosswalk path')
        path=Path(crosswalk or default);g=Graph().parse(path)
        self.version=hashlib.sha256(sources.read_bytes()+path.read_bytes()+b'iati-search-mentions-1').hexdigest()
        self.candidate_review=False;self.rules={}
        allowed=set(registry.objects(SOURCE,UE.fieldMapping))
        for entry in g.subjects(RDF.type,UE.CrosswalkEntry):
            if one(g,entry,IQ.reviewStatus)!='approved':continue
            for pred in (DCTERMS.creator,DCTERMS.date,DCTERMS.description,DCTERMS.source):one(g,entry,pred)
            axis=one(g,entry,IQ.axis);concept=one(g,entry,IQ.concept)
            if axis not in AXES:raise ValueError('Unsupported axis')
            actors.concept(concept,axis)
            if one(g,entry,IQ.matchKind)!='mention':raise ValueError('Only reviewed narrative mention mappings are enabled')
            phrases=sorted(str(x) for x in g.objects(entry,IQ.phrase))
            if not phrases or len(phrases)>30 or any(not p or len(p)>120 for p in phrases):raise ValueError('Invalid bounded phrases')
            bindings=[]
            for node in g.objects(entry,IQ.binding):
                if node not in allowed or AXES[axis] not in registry.objects(node,UE.feedsAxis):raise ValueError('Binding does not support axis')
                if one(registry,node,IQ.valueKind)!='narrative':raise ValueError('Code bindings are metadata only until a code crosswalk is reviewed')
                field=one(registry,node,IQ.queryField);source=one(registry,node,UE.sourceField)
                if not re.fullmatch(r'[a-z][a-z0-9_]*',field) or source not in ('title','description'):raise ValueError('Unsupported narrative binding')
                bindings.append({'id':str(node),'field':field,'source':source})
            if not bindings:raise ValueError('Missing source binding')
            if (axis,concept) in self.rules:raise ValueError('Duplicate concept mapping')
            self.rules[axis,concept]={'concept':concept,'entry':str(entry),'phrases':phrases,'bindings':sorted(bindings,key=lambda b:b['id'])}
    def get(self,axis,uri):return self.rules.get((axis,uri)) if uri else None
    def query(self,rule):
        return '('+' OR '.join(b['field']+':'+quote(p) for b in rule['bindings'] for p in rule['phrases'])+')'
    def support(self,raw,rule):
        found=[]
        for b in rule['bindings']:
            for i,entry in enumerate(raw.get(b['source'],[])):
                for j,n in enumerate(entry.get('narrative',[])):
                    text=n.get('text()','')
                    if not isinstance(text,str):continue
                    matched=[p for p in rule['phrases'] if contains(text,p)]
                    if not matched:continue
                    # Excerpts are source text, not generated interpretations. Include each matched phrase.
                    for phrase in matched:
                        hit=re.search(re.escape(phrase),text,re.I)
                        if not hit:continue
                        left=max(0,hit.start()-160);right=min(len(text),hit.end()+220)
                        found.append({'kind':'mention','concept':rule['concept'],'crosswalk':rule['entry'],
                            'binding':b['id'],'path':[b['source'],i,'narrative',j,'text()'],
                            'phrase':phrase,'text':text[left:right],'excerpt_start':left,'excerpt_end':right})
        return found[:12]


def select_projects(items):
    ended={i['id'] for i in items if i['freshness']['status']=='ended'}
    selected={};excluded=[];fingerprints={}
    for item in items:fingerprints.setdefault(item['id'],set()).add(item['fingerprint'])
    for item in items:
        if item['id'] in ended:excluded.append({'activity':item['id'],'reason':'ended'});continue
        if not all(m['support'] for m in item['matches'].values()):
            excluded.append({'activity':item['id'],'reason':'search-terms-not-found-in-returned-fields'});continue
        p=project(item);p['participants']=[]
        for v in item['source']['participants']:
            if v['identity_status']=='withheld':continue
            names=[n for n in v['names'] if n.strip() and not actors.withheld_name(n)]
            if not names and not v['organization']:continue
            p['participants'].append({'names':names,'ref':v['ref'] if v['organization'] else None,
                'organization':v['organization'],'role':v['role_label'],'identity_status':v['identity_status']})
        p['reporting_organizations']=[{'names':actors.narratives([v]),'ref':v.get('@ref')} for v in item['source']['publisher']]
        if len(fingerprints[item['id']])>1:
            p['freshness']=deepcopy(p['freshness']);p['freshness']['status']='unverified'
            p['freshness']['issues']=sorted(set(p['freshness']['issues']+['conflicting-source-reports']))
        # Stable representative; do not merge participants across conflicting snapshots.
        if p['id'] not in selected or p['source_fingerprint']<selected[p['id']]['source_fingerprint']:selected[p['id']]=p
    return sorted(selected.values(),key=lambda p:(p['freshness']['status']!='consistent-current',p['id'])),excluded


def projects_rdf(projects,version):
    g=Graph();g.bind('prov',actors.PROV);g.bind('dcterms',DCTERMS)
    for p in projects:
        act=URIRef(p['uri']);snap=URIRef(p['uri']+'/live/'+p['source_fingerprint'])
        g.add((act,RDF.type,actors.PROV.Activity));g.add((act,DCTERMS.title,Literal(p['title'])))
        g.add((snap,RDF.type,actors.PROV.Entity));g.add((snap,DCTERMS.references,act))
        g.add((snap,actors.PROV.wasDerivedFrom,URIRef(p['source_url'])))
        g.add((snap,actors.PROV.generatedAtTime,Literal(p['retrieved_at'],datatype=XSD.dateTime)))
        for role,parties in [('reporting',p['reporting_organizations'])]+[(v['role'],[v]) for v in p['participants']]:
            for party in parties:
                ref=party.get('ref');identified=bool(ref and actors.usable_ref(ref))
                node=URIRef(actors.org_uri(ref)) if identified else BNode()
                if identified:g.add((node,RDF.type,actors.PROV.Organization));g.add((node,DCTERMS.identifier,Literal(ref)))
                for name in party['names']:g.add((node,RDFS.label,Literal(name)))
                assoc=BNode();g.add((snap,actors.PROV.qualifiedAssociation,assoc));g.add((assoc,RDF.type,actors.PROV.Association))
                g.add((assoc,actors.PROV.hadRole,URIRef(actors.ACTIVITIES+'roles/'+role)))
                # A missing identity stays a source-scoped mention, not a canonical organization.
                g.add((assoc,actors.PROV.agent if identified else DCTERMS.references,node))
        for m in p['matches'].values():
            for support in m['support']:
                node=BNode();g.add((node,RDF.type,actors.PROV.Entity));g.add((node,actors.PROV.wasDerivedFrom,snap))
                g.add((node,DCTERMS.description,Literal(actors.canonical(support))))
                g.add((node,DCTERMS.references,URIRef(support['crosswalk'])))
                g.add((node,DCTERMS.hasVersion,Literal(version)))
    return g.serialize(format='turtle')


class ProjectSearch(Organizations):
    def __init__(self,rules=None,*args,**kwargs):super().__init__(rules or SearchCrosswalk(),*args,**kwargs)
    def envelope(self,*args,**kwargs):
        result=super().envelope(*args,**kwargs)
        result.update(mode='project-mentions',projects=[],returned_project_count=0,matched_project_count=0)
        return result
    async def execute(self,state,intervention,context):
        start=self.clock();result=self.envelope(state,intervention,'ok');rules=[];items=[]
        for axis,uri in [('state',state),('intervention',intervention)]:
            if not uri:continue
            rule=self.rules.get(axis,uri)
            if not rule:result.update(status='missing-mappings');return result
            rules.append((axis,rule))
        if not rules:result.update(status='missing-context');return result
        q=' AND '.join(self.rules.query(rule) for _,rule in rules)
        record={'kind':'primary','query':q,'rows':50,'returned':0,'status':'pending'}
        result['coverage']['queries']=[record];retrieval=matching=0
        try:
            async with asyncio.timeout(max(.01,self.deadline-.2)):
                t=self.clock()
                try:
                    if self.fetcher:payload=await self.fetcher(q)
                    else:
                        if self.quota is None:self.quota=Quota()
                        payload=await fetch_live(q,self.quota)
                finally:retrieval=self.clock()-t
                docs,total,truncated=unpack(payload)
                record.update(status='ok',returned=len(docs),total=total,truncated=truncated)
                result['coverage']['truncated']=truncated
                t=self.clock()
                for doc in docs:
                    await asyncio.sleep(0);result['coverage']['examined']+=1
                    try:
                        item=activity(doc,result['retrieved_at'],None,None)
                        raw=doc['iati_json'];raw=json.loads(raw) if isinstance(raw,str) else raw;raw=raw['iati-activity'][0]
                        item['matches']={axis:{'support':self.rules.support(raw,rule)} for axis,rule in rules}
                        items.append(item)
                    except (ValueError,KeyError,TypeError,AttributeError,IndexError):result['errors'].append('invalid-activity')
                matching=self.clock()-t
        except (SourceError,TimeoutError,OSError,sqlite3.Error) as e:
            result['errors'].append(str(e) if isinstance(e,SourceError) else 'endpoint-deadline' if isinstance(e,TimeoutError) else 'source-or-limiter-unavailable')
            if record['status']=='pending':record['status']='error'
        t=self.clock();projects,excluded=select_projects(items)
        result.update(projects=projects[:5],returned_project_count=min(5,len(projects)),matched_project_count=len(projects),excluded=excluded)
        result['coverage']['project_limit_reached']=len(projects)>5
        result['rdf']=projects_rdf(result['projects'],self.rules.version)
        if len(json.dumps(result).encode())>MAX_RESPONSE_BYTES:
            result['rdf']='';result['errors'].append('response-size-limit');result['coverage']['truncated']=True
        result['status']='partial' if result['errors'] and items else 'error' if result['errors'] else 'ok' if projects else 'insufficient-matches' if items else 'empty'
        result['timings']={'source_seconds':retrieval,'matching_seconds':matching,'graph_and_selection_seconds':self.clock()-t,'total_seconds':self.clock()-start}
        return result
