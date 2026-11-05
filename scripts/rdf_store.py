"""Authoritative Turtle with separate, recoverable JSON workflow state.

Only revision history/structured field values use JSON literals, never a hidden
whole-record payload. Domain identifiers and measurement links are RDF resources.
A write-ahead journal protects the TTL + workflow pair; readers fail closed during
an interrupted publication until a writer recovers it. No database dependency.
"""
from contextlib import contextmanager
from copy import deepcopy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
from urllib.parse import quote
from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import RDF, DCTERMS

UE=Namespace('https://universalevidence.com/ontology/')
STORE=URIRef('https://universalevidence.com/storage/metadata')
CLASSES={'studies':UE.Study,'documents':UE.ReportDocument,'findings':UE.OutcomeMeasurement,
         'activities':UE.ActivityRecord,'populations':UE.ActivityPopulation,'events':UE.RevisionEvent}
FIELD_MAP={'studies':{'title':DCTERMS.title},
 'documents':{'identifier':DCTERMS.identifier,'citation':DCTERMS.bibliographicCitation,'url':DCTERMS.source,'kind':DCTERMS.type,'version':DCTERMS.hasVersion,'status':UE.reportStatus,'status_check':UE.statusCheck},
 'findings':{'study_id':UE.measurementStudy,'state_uri':UE.ofState,'indicator_uri':UE.ofIndicator,'status':UE.measurementStatus}}
LEGACY_FINDING_FIELDS={'study_id':UE.findingStudy,'state_uri':UE.outcomeState,'indicator_uri':UE.findingIndicator,'status':UE.findingStatus}
URI_FIELDS={'study_id','state_uri','indicator_uri','url','uri'}
JSON_FIELDS={'records','axes','context','planned_outcomes','status_check','study_links','comparison','timing','uncertainty','mapping','sources','derivation','lineage_review','source','mappings','resolutions','freshness_errors','withdrawal','event','snapshot','batch','activity_ids','discovery','errors','queries','interventions','in_flight'}

def canonical(v):return json.dumps(v,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False)
def predicate(table,key):return FIELD_MAP.get(table,{}).get(key,UE[key])
def digest(v):return hashlib.sha256(canonical(v).encode()).hexdigest()

def atomic_write(path,data):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    fd,name=tempfile.mkstemp(prefix='.'+path.name+'.',dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as f:f.write(data);f.flush();os.fsync(f.fileno())
        os.replace(name,path)
        fd=os.open(path.parent,os.O_RDONLY)
        try:os.fsync(fd)
        finally:os.close(fd)
    finally:
        if os.path.exists(name):os.unlink(name)

class StoreError(ValueError):pass

class Store:
    def __init__(self,path,schema,projection=None,*,readonly=False):
        self.path=Path(path).resolve()
        if self.path.suffix!='.ttl':raise ValueError('Authoritative store must be a .ttl file; migrate legacy SQLite explicitly')
        self.schema=schema;self.projection=projection;self.readonly=readonly
        self.workflow_path=self.path.with_name('.'+self.path.stem+'.workflow.json')
        self.journal=self.path.with_name('.'+self.path.stem+'.transaction.json')
        self.lock_path=self.path.with_name('.'+self.path.stem+'.lock')
        self.data={t:{} for t in CLASSES};self.meta={'schema':schema,'version':'empty','generation':'0'};self.workflow={};self.read_deadline=float('inf')
        self._lock=None;self._before=None;self.revision='empty'
        self.load()
    def load(self):
        if self.journal.exists():raise StoreError('Interrupted RDF transaction; run a writer recovery before reading')
        if not self.path.exists():
            if self.readonly:raise FileNotFoundError(self.path)
            return
        raw=self.path.read_bytes();self.revision=hashlib.sha256(raw).hexdigest()
        try:g=Graph().parse(data=raw,format='turtle')
        except Exception as exc:raise StoreError('Invalid canonical Turtle') from exc
        if self.journal.exists():raise StoreError('Interrupted RDF publication; retry after writer recovery')
        schema=str(g.value(STORE,UE.storageSchema) or '')
        if schema!=self.schema:raise StoreError('Unsupported RDF store schema')
        self.meta=json.loads(str(g.value(STORE,UE.storageMetadata)))
        self.data={t:{} for t in CLASSES}
        for table,cls in CLASSES.items():
            for node in set(g.subjects(RDF.type,cls)) | (set(g.subjects(RDF.type,UE.ReportedFinding)) if table=='findings' else set()):
                manifest=g.value(node,UE.storedFields)
                if manifest is None:continue # derived public projection node
                spec=json.loads(str(manifest));obj={}
                for key,kind in spec.items():
                    vals=list(g.objects(node,predicate(table,key)))
                    if table=='findings' and key in LEGACY_FINDING_FIELDS:
                        vals=list(set(vals) | set(g.objects(node,LEGACY_FINDING_FIELDS[key])))
                    if len(vals)!=1:raise StoreError('Ambiguous/missing canonical RDF field: '+str(node)+' '+key)
                    v=vals[0]
                    if kind=='json':v=json.loads(str(v))
                    elif kind=='uri':v=str(v)
                    else:v=v.toPython()
                    obj[key]=v
                self.data[table][obj['id']]=obj
        if not self.readonly and not self.workflow_path.exists():raise StoreError('Operational sidecar missing; restore the paired backup before writing')
        if not self.readonly and self.workflow_path.exists():
            body=json.loads(self.workflow_path.read_text())
            if body['knowledge_sha256']!=hashlib.sha256(self.path.read_bytes()).hexdigest():raise StoreError('Workflow and RDF revision differ; inspect before writing')
            self.workflow=body['workflow']
    def graph(self):
        g=Graph();g.bind('ue',UE);g.bind('dcterms',DCTERMS)
        g.add((STORE,UE.storageSchema,Literal(self.schema)));g.add((STORE,UE.storageMetadata,Literal(canonical(self.meta))))
        for table,cls in CLASSES.items():
            for identity,obj in sorted(self.data[table].items()):
                node=URIRef(identity if identity.startswith(('http://','https://')) else 'https://universalevidence.com/storage/'+table+'/'+quote(identity,safe=''))
                g.add((node,RDF.type,cls));fields={}
                for key,v in obj.items():
                    if isinstance(v,(dict,list)) or v is None:kind='json';value=Literal(canonical(v))
                    elif key in URI_FIELDS:kind='uri';value=URIRef(v)
                    else:kind='literal';value=Literal(v)
                    fields[key]=kind;g.add((node,predicate(table,key),value))
                g.add((node,UE.storedFields,Literal(canonical(fields))))
        if self.projection:
            extra=self.projection(self)
            # JSON serialization of semantic fields must agree with stored fields.
            g+=extra
            # Graph union copies triples, not the projection's namespace bindings.
            bound_namespaces={namespace for _,namespace in g.namespaces()}
            for prefix,namespace in extra.namespaces():
                if namespace not in bound_namespaces:
                    g.bind(prefix,namespace)
                    bound_namespaces.add(namespace)
        return g
    def recover(self):
        if not self.journal.exists():return
        tx=json.loads(self.journal.read_text())
        if tx['schema']!=self.schema:raise StoreError('Unexpected transaction schema')
        atomic_write(self.workflow_path,tx['workflow'].encode())
        atomic_write(self.path,tx['ttl'].encode())
        self.journal.unlink()
    def __enter__(self):
        if self.readonly:raise StoreError('Cannot write a read-only RDF snapshot')
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self._lock=self.lock_path.open('a+b');fcntl.flock(self._lock,fcntl.LOCK_EX)
        try:
            self.recover();self.load();self._before=deepcopy((self.data,self.meta,self.workflow));return self
        except BaseException:self._unlock();raise
    def _unlock(self):
        if self._lock:fcntl.flock(self._lock,fcntl.LOCK_UN);self._lock.close();self._lock=None
    def commit(self):
        if self.readonly:raise StoreError('Read-only snapshot')
        owned=self._lock is not None
        if not owned:
            self.path.parent.mkdir(parents=True,exist_ok=True);self._lock=self.lock_path.open('a+b');fcntl.flock(self._lock,fcntl.LOCK_EX)
            if self.journal.exists():self._unlock();raise StoreError('Recover interrupted transaction before committing')
        try:
            ttl=self.graph().serialize(format='turtle');Graph().parse(data=ttl,format='turtle')
            workflow=canonical({'knowledge_sha256':hashlib.sha256(ttl.encode()).hexdigest(),'workflow':self.workflow})+'\n'
            atomic_write(self.journal,(canonical({'schema':self.schema,'ttl':ttl,'workflow':workflow})+'\n').encode())
            self.recover();self.revision=hashlib.sha256(ttl.encode()).hexdigest()
        finally:
            if not owned:self._unlock()
    def __exit__(self,kind,value,tb):
        try:
            if kind is None:self.commit()
            elif self._before:self.data,self.meta,self.workflow=self._before
        finally:self._before=None;self._unlock()
    def close(self):self._unlock()

@contextmanager
def transaction(path,schema,projection):
    # Recovery precedes loading: constructor refuses pending transactions.
    path=Path(path).resolve();path.parent.mkdir(parents=True,exist_ok=True)
    lock=path.with_name('.'+path.stem+'.lock')
    with lock.open('a+b') as f:
        fcntl.flock(f,fcntl.LOCK_EX)
        journal=path.with_name('.'+path.stem+'.transaction.json')
        if journal.exists():
            tx=json.loads(journal.read_text())
            if tx['schema']!=schema:raise StoreError('Unexpected transaction schema')
            atomic_write(path.with_name('.'+path.stem+'.workflow.json'),tx['workflow'].encode());atomic_write(path,tx['ttl'].encode());journal.unlink()
        store=Store(path,schema,projection);store._lock=f
        try:
            yield store
            store.commit()
        finally:store._lock=None

@contextmanager
def snapshot(path,schema,projection,budget=None):
    start=time.monotonic();store=Store(path,schema,projection,readonly=True)
    if budget is not None:store.read_deadline=start+budget
    if time.monotonic()>store.read_deadline:raise StoreError('enrichment_read_budget')
    yield store

def backup(path,destination,schema,projection):
    destination=Path(destination)
    if destination.exists() or destination.with_name('.'+destination.stem+'.workflow.json').exists():raise ValueError('Backup destination must be new')
    with transaction(path,schema,projection) as store:
        copy=Store(destination,schema,projection);copy.data=deepcopy(store.data);copy.meta=deepcopy(store.meta);copy.workflow=deepcopy(store.workflow)
        copy.commit()
    return hashlib.sha256(destination.read_bytes()).hexdigest()
