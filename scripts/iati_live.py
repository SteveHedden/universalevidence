"""On-demand transient organization lookup. No source data writes or retries."""
from __future__ import annotations
import asyncio
from collections import OrderedDict
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid
from urllib.parse import quote
import httpx
from rdflib import Graph, Literal, URIRef
from rdflib.namespace import RDF, RDFS, DCTERMS, XSD
from scripts import iati_actors as actors
from scripts.iati_live_rules import RuleBook, activity, query, safe_url

ENDPOINT = 'https://api.iatistandard.org/datastore/activity/select'
MAX_BYTES = 5_000_000
MAX_RESPONSE_BYTES = 5_000_000


class SourceError(Exception):
    """Only a fixed public error code is propagated, never source headers/bodies."""


class Quota:
    """Atomic host-wide leases and rolling budgets; stores only counters, no key/data.

    All workers using a subscription must share this file. Default Exploratory
    limits: 1/sec, 500/rolling day. Count every attempt conservatively. Leases expire
    after the source timeout plus margin so process crashes cannot strand slots.
    """
    def __init__(self, path=None, *, interval=None, daily=None, concurrency=2):
        self.path = str(path or os.getenv('IATI_LIMITER_DB', '/tmp/ue-iati-quota.sqlite3'))
        self.interval = interval if interval is not None else max(1., 60 / float(os.getenv('IATI_REQUESTS_PER_MINUTE','60')))
        self.daily = daily if daily is not None else int(os.getenv('IATI_REQUESTS_PER_DAY','500'))
        self.concurrency = min(2, concurrency)
        if self.interval<0 or self.daily<1 or self.concurrency<1: raise ValueError('Invalid IATI quota configuration')
        with sqlite3.connect(self.path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS calls (id TEXT PRIMARY KEY, started REAL, lease REAL)')
            db.execute('CREATE TABLE IF NOT EXISTS cooldown (id INTEGER PRIMARY KEY, until REAL)')

    def reserve(self):
        now = time.time()
        with sqlite3.connect(self.path, timeout=0.05) as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM calls WHERE started < ? AND lease < ?', (now-86400, now))
            count, last, active = db.execute('SELECT count(*), max(started), sum(lease>?) FROM calls', (now,)).fetchone()
            cooldown = db.execute('SELECT until FROM cooldown WHERE id=1').fetchone()
            if cooldown and cooldown[0]>now: return None
            if count>=self.daily: raise SourceError('quota-exhausted')
            if (active or 0)>=self.concurrency or (last is not None and now-last<self.interval): return None
            token=uuid.uuid4().hex
            db.execute('INSERT INTO calls VALUES (?,?,?)', (token, now, now+5))
            return token

    async def acquire(self):
        while True:
            try: token=self.reserve()
            except sqlite3.OperationalError: token=None
            if token: return token
            await asyncio.sleep(.025)

    def release(self, token):
        with sqlite3.connect(self.path, timeout=.1) as db:
            db.execute('UPDATE calls SET lease=0 WHERE id=?', (token,))

    def pause(self, seconds):
        with sqlite3.connect(self.path, timeout=.1) as db:
            db.execute('INSERT INTO cooldown VALUES (1,?) ON CONFLICT(id) DO UPDATE SET until=max(until,excluded.until)', (time.time()+seconds,))


async def fetch_live(q, quota):
    key=os.getenv('IATI_API_KEY')
    if not key: raise SourceError('source-not-configured')
    token=await quota.acquire()
    try:
        async with asyncio.timeout(4):
            async with httpx.AsyncClient(timeout=4, follow_redirects=False) as client:
                async with client.stream('GET', ENDPOINT, params={'q':q,'rows':50,'start':0,'fl':'iati_identifier,iati_json','sort':'iati_identifier asc','wt':'json'}, headers={'Ocp-Apim-Subscription-Key':key,'Accept':'application/json'}) as response:
                    if response.status_code in (429,403):
                        try: pause=max(1,min(86400,int(response.headers.get('Retry-After','60'))))
                        except ValueError: pause=60
                        quota.pause(pause)
                        raise SourceError(f'source-rate-or-quota-limit-{response.status_code}')
                    if response.status_code!=200: raise SourceError(f'source-http-{response.status_code}')
                    body=bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body)>MAX_BYTES: raise SourceError('source-response-too-large')
                    try: return json.loads(body)
                    except (ValueError, UnicodeError): raise SourceError('invalid-source-response') from None
    except (httpx.HTTPError, TimeoutError):
        raise SourceError('source-timeout-or-network-error') from None
    finally:
        quota.release(token)


def unpack(payload):
    try:
        response=payload['response']; docs=response['docs']; total=response['numFound']
        if not isinstance(docs,list) or type(total) is not int or total<0: raise ValueError()
        header=payload.get('responseHeader',{})
        if header.get('status',0)!=0: raise ValueError()
        partial=header.get('partialResults') in (True,'true') or response.get('numFoundExact') is False
        return docs[:50], total, partial or total>50 or len(docs)>50
    except (KeyError, TypeError, ValueError):
        raise SourceError('invalid-source-response') from None


def card_rank(card):
    return (card['relevance']!='explicit', card['representative']['freshness']['status']!='consistent-current', card['id'])


def project(item):
    source=item['source']
    titles=actors.narratives(source['titles'])
    countries=[]
    for c in source['geography'].get('recipient-country',[]):
        names=actors.narratives([c])
        countries.append({'code':c.get('@code'),'name':names[0] if names else c.get('@code','Unknown country')})
    return {'id':item['id'], 'uri':item['uri'], 'title':titles[0] if titles else 'Project title unavailable',
            'source_url':item['source_url'], 'project_url':item['project_url'], 'countries':countries,
            'publisher':source['publisher'], 'retrieved_at':item['retrieved_at'], 'freshness':item['freshness'],
            'matches':item['matches'], 'sectors':source['sectors'], 'source_fingerprint':item['fingerprint']}


def collect(items, state, intervention):
    organizations={}; unresolved={}; excluded=[]
    ended_ids={item['id'] for item in items if item['freshness']['status']=='ended'}
    fingerprints={}
    for item in items: fingerprints.setdefault(item['id'],set()).add(item['fingerprint'])
    for item in items:
        match=item['matches']
        if item['id'] in ended_ids:
            excluded.append({'activity':item['id'],'reason':'ended'}); continue
        explicit=all(m['explicit'] for m in match.values())
        candidate=bool(state and intervention and match['intervention']['explicit'] and match['state']['relevant'])
        if not explicit and not candidate:
            excluded.append({'activity':item['id'],'reason':'insufficiently-specific-match','matches':match});continue
        relevance='explicit' if explicit else 'candidate'
        p=project(item); p['relevance']=relevance
        if len(fingerprints[item['id']])>1:
            p=deepcopy(p)
            p['freshness']['status']='unverified'
            p['freshness']['issues']=sorted(set(p['freshness']['issues']+['conflicting-source-reports']))
        eligible=False
        for participant in item['source']['participants']:
            if participant['role']!='4':continue
            names=[n for n in participant['names'] if n.strip() and not actors.withheld_name(n) and n.strip().casefold() not in ('unknown','n/a','na','none','null','tbd','implementing partner','not applicable','not known','unknown organization','unknown organisation','unnamed','to be determined')]
            if participant['identity_status']=='withheld' or not names:continue
            if participant['identity_status']!='identified':
                unresolved[participant['id']]={'id':participant['id'],'activity':item['id'],'names':names,'role':'4','publisher':participant['publisher'],'identity_status':'unresolved'}
                continue
            eligible=True
            oid=participant['organization']
            if oid not in organizations:
                organizations[oid]={'id':oid,'ref':participant['ref'],'source':'IATI','name':names[0],'aliases':[],'relevance':relevance,'activities':[]}
            card=organizations[oid]
            card['aliases']=sorted(set(card['aliases']+names))
            # Duplicate reports of the same activity do not inflate counts.
            if not any(x['id']==p['id'] and x['source_fingerprint']==p['source_fingerprint'] for x in card['activities']):
                card['activities'].append(p)
            if explicit:card['relevance']='explicit'
        if not eligible:excluded.append({'activity':item['id'],'reason':'no-named-identified-implementer'})
    for card in organizations.values():
        card['activities'].sort(key=lambda p:(p['relevance']!='explicit',p['freshness']['status']!='consistent-current',p['id'],p['source_fingerprint']))
        card['representative']=card['activities'][0]
        card['name']=card['aliases'][0]
    cards=sorted(organizations.values(),key=card_rank)
    return cards, list(unresolved.values()), excluded


def transient_graph(cards, rules, retrieved_at):
    graph=Graph(); graph.bind('prov',actors.PROV); graph.bind('dcterms',DCTERMS)
    for card in cards:
        org=URIRef(card['id']);graph.add((org,RDF.type,actors.PROV.Organization))
        graph.add((org,RDFS.label,Literal(card['name'])));graph.add((org,DCTERMS.identifier,Literal(card['ref'])))
        for project in card['activities']:
            act=URIRef(project['uri']); snapshot=URIRef(str(act)+'/live/'+project['source_fingerprint'])
            graph.add((act,RDF.type,actors.PROV.Activity));graph.add((act,DCTERMS.title,Literal(project['title'])))
            graph.add((act,DCTERMS.identifier,Literal(project['id'])))
            graph.add((snapshot,RDF.type,actors.PROV.Entity));graph.add((snapshot,DCTERMS.references,act))
            graph.add((snapshot,actors.PROV.generatedAtTime,Literal(retrieved_at,datatype=XSD.dateTime)))
            graph.add((snapshot,actors.PROV.wasDerivedFrom,URIRef(project['source_url'])))
            association=URIRef(str(snapshot)+'/associations/'+actors.digest(card['id']))
            graph.add((act,actors.PROV.qualifiedAssociation,association));graph.add((association,RDF.type,actors.PROV.Association))
            graph.add((association,actors.PROV.agent,org));graph.add((association,actors.PROV.hadRole,URIRef(actors.ACTIVITIES+'roles/4')))
            graph.add((association,DCTERMS.source,snapshot))
            for m in project['matches'].values():
                for support in m['support']:
                    assertion=URIRef(str(snapshot)+'/support/'+actors.digest(support))
                    graph.add((assertion,RDF.type,actors.PROV.Entity));graph.add((assertion,actors.PROV.wasDerivedFrom,snapshot))
                    graph.add((assertion,DCTERMS.description,Literal(actors.canonical(support))))
                    if support['kind']=='objective' and m['explicit']:
                        target=URIRef(support['concept']);graph.add((act,DCTERMS.subject,target))
                        for pred,value in ((RDF.type,RDF.Statement),(RDF.subject,act),(RDF.predicate,DCTERMS.subject),(RDF.object,target)):
                            graph.add((assertion,pred,value))
    # This graph is deliberately never inserted into Fuseki or an actor store.
    return graph.serialize(format='turtle')


class Organizations:
    def __init__(self, rules=None, fetcher=None, quota=None, *, deadline=8, cache_size=500, clock=time.monotonic):
        self.rules=rules or RuleBook();self.quota=quota
        self.fetcher=fetcher; self.deadline=deadline; self.cache_size=cache_size;self.clock=clock
        self.cache=OrderedDict();self.inflight={}

    async def lookup(self, state='', intervention='', context=''):
        key=(state,intervention,context,self.rules.version)
        now=self.clock()
        if key in self.cache:
            expiry,result=self.cache[key]
            if expiry>now:
                self.cache.move_to_end(key);result=deepcopy(result);result['cache']='hit';return result
            del self.cache[key]
        if key not in self.inflight:
            if len(self.inflight)>=100: return self.envelope(state,intervention,'busy',errors=['request-capacity'])
            task=asyncio.create_task(self.execute(state,intervention,context))
            self.inflight[key]=task
            def done(task):
                self.inflight.pop(key,None)
                if task.cancelled() or task.exception():return
                result=task.result()
                if result['status'] in ('ok','empty','insufficient-matches') and not result['errors'] and not result['coverage']['truncated']:
                    ttl=600 if result['organizations'] or result.get('projects') else 60
                    self.cache[key]=(self.clock()+ttl,deepcopy(result))
                    while len(self.cache)>self.cache_size:self.cache.popitem(last=False)
            task.add_done_callback(done)
        return deepcopy(await asyncio.shield(self.inflight[key]))

    def envelope(self,state,intervention,status,errors=None):
        return {'status':status,'organizations':[],'unresolved_participants':[],'excluded':[],
                'identified_organization_count':0,'returned_organization_count':0,
                'missing_dimensions':[a for a,v in [('condition',state),('intervention',intervention)] if not v],
                'errors':errors or [],'coverage':{'queries':[],'truncated':False,'exhaustive':False,'examined':0},
                'retrieved_at':datetime.now(timezone.utc).isoformat(),'mapping_version':self.rules.version,
                'candidate_mapping_review':self.rules.candidate_review,'timings':{},'cache':'miss','rdf':''}

    async def execute(self,state,intervention,context):
        start=self.clock();result=self.envelope(state,intervention,'ok'); items=[];seen=set()
        state_rule=self.rules.get('state',state); intervention_rule=self.rules.get('intervention',intervention)
        missing=[uri for uri,rule in [(state,state_rule),(intervention,intervention_rule)] if uri and not rule]
        if missing:
            result.update(status='missing-mappings',missing_mappings=missing);return result
        if not state and not intervention:
            result.update(status='missing-context');return result
        primary=' AND '.join(query(rule) for rule in [state_rule,intervention_rule] if rule)
        queries=[('primary',primary)]
        if state and intervention:queries.append(('fallback',query(intervention_rule)))
        retrieval=matching=0.
        try:
            # Reserve a small part of the eight-second envelope for serializing
            # bounded successful candidates after a source deadline expires.
            async with asyncio.timeout(max(.01,self.deadline-.2)):
                for kind,q in queries:
                    if kind=='fallback':
                        cards,_,_=collect(items,state,intervention)
                        if any(c['relevance']=='explicit' for c in cards):break
                        if result['errors']:break  # Failure is not evidence of zero matches.
                    record={'kind':kind,'query':q,'rows':50,'returned':0,'status':'pending'}
                    result['coverage']['queries'].append(record)
                    t=self.clock()
                    try:
                        if self.fetcher: payload=await self.fetcher(q)
                        else:
                            if self.quota is None:self.quota=Quota()
                            payload=await fetch_live(q,self.quota)
                        docs,total,truncated=unpack(payload)
                    except SourceError as exc:
                        record['status']='error';result['errors'].append(str(exc));break
                    finally:retrieval+=self.clock()-t
                    record.update(status='ok',returned=len(docs),total=total,truncated=truncated)
                    result['coverage']['truncated'] |= truncated
                    t=self.clock()
                    for doc in docs:
                        await asyncio.sleep(0)
                        result['coverage']['examined']+=1
                        try:
                            item=activity(doc,result['retrieved_at'],state_rule,intervention_rule)
                        except (ValueError, TypeError, KeyError, AttributeError, IndexError):
                            result['errors'].append('invalid-activity');continue
                        identity=(item['id'],item['fingerprint'])
                        if identity not in seen:items.append(item);seen.add(identity)
                    matching+=self.clock()-t
        except TimeoutError:
            result['errors'].append('endpoint-deadline')
            for record in result['coverage']['queries']:
                if record['status']=='pending':record['status']='timeout'
        except (OSError, sqlite3.Error):
            result['errors'].append('limiter-unavailable')
        t=self.clock()
        cards,unresolved,excluded=collect(items,state,intervention)
        # Candidate group is used only when no eligible explicit organization exists.
        if any(c['relevance']=='explicit' for c in cards):cards=[c for c in cards if c['relevance']=='explicit']
        result.update(organizations=cards[:5],unresolved_participants=unresolved,unresolved_participant_count=len(unresolved),excluded=excluded,
                      identified_organization_count=len(cards),returned_organization_count=min(5,len(cards)))
        result['coverage']['card_limit_reached']=len(cards)>5
        result['rdf']=transient_graph(result['organizations'],self.rules,result['retrieved_at'])
        if len(json.dumps(result).encode()) > MAX_RESPONSE_BYTES:
            # Keep representative projects and counts; disclose omitted detail.
            result['coverage']['truncated']=True
            result['coverage']['response_details_truncated']=True
            result['unresolved_participant_count']=len(unresolved)
            result['unresolved_participants']=unresolved[:20]
            result['excluded']=[{'activity':e['activity'],'reason':e['reason']} for e in excluded]
            for card in result['organizations']: card['activities']=[card['representative']]
            result['rdf']=transient_graph(result['organizations'],self.rules,result['retrieved_at'])
            if len(json.dumps(result).encode()) > MAX_RESPONSE_BYTES:
                result['rdf']=''
                result['errors'].append('response-size-limit')
        graph_time=self.clock()-t
        if result['errors']:result['status']='partial' if cards or items else 'error'
        elif not cards:result['status']='insufficient-matches' if items else 'empty'
        result['timings']={'source_seconds':retrieval,'matching_seconds':matching,'graph_and_selection_seconds':graph_time,'total_seconds':self.clock()-start}
        return result
