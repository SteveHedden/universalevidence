"""Maintained report-result semantics: validated worker writes, indexed runtime reads."""
import hashlib
from functools import lru_cache
from pathlib import Path
from rdflib import Graph, RDF, URIRef, Literal, Namespace, BNode
try:
 from .result_crosswalk import execute,R,F,U,P
except ImportError:
 from result_crosswalk import execute,R,F,U,P
A=Namespace('https://universalevidence.com/mapping-rules/aggregation/')
PROV=Namespace('http://www.w3.org/ns/prov#')

def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def fingerprint(g):
 if any(isinstance(v,BNode) for t in g for v in t):raise ValueError('Named nodes required for stable mapping identities')
 return hashlib.sha256('\n'.join(sorted(' '.join(v.n3() for v in t) for t in g)).encode()).hexdigest()

def closure(g,root):
 out=Graph();pending=[root];seen=set()
 while pending:
  node=pending.pop()
  if node in seen:continue
  seen.add(node)
  for t in g.triples((node,None,None)):
   out.add(t)
   if isinstance(t[2],URIRef) and any(g.triples((t[2],None,None))):pending.append(t[2])
 return out

def validate(proposal,evidence,taxonomy,registry,interpretation,study_uri):
 fingerprint(proposal)
 mappings=set(proposal.subjects(RDF.type,R.ResultMapping))
 if not mappings:raise ValueError('No result mappings')
 allowed_predicates={RDF.type,P.intervention,P.interventionGroup,P.comparatorGroup,U.taxonomyConcept,A.summaryStatus,A.selectionReason,F.comparison,F.extractor,F.indicator,F.interpretation,F.source_result,F.state,F.study_mapping,F.assigned_components_complete,F.description,F.shared_background,F.control_kind,F.source_kind,F.context_sha256,F.source,F.study,F.subject,F.selected,F.orientation,F.estimand,F.model_context}
 if any(p not in allowed_predicates for _,p,_ in proposal):raise ValueError('Unsupported semantic predicate; source values and rules belong elsewhere')
 dependencies=Graph()
 for m in mappings:dependencies+=closure(proposal,m)
 if set(dependencies)!=set(proposal):raise ValueError('Unreachable semantic definitions')
 seen=set()
 for m in mappings:
  sm=proposal.value(m,F.study_mapping);source=proposal.value(m,F.source_result)
  if proposal.value(sm,F.study)!=URIRef(study_uri) or str(proposal.value(sm,F.source_kind))!='local-rdf':raise ValueError('Mapping outside owned report study')
  if source in seen:raise ValueError('Duplicate finding selection')
  seen.add(source)
  if evidence.value(source,U.measurementStudy)!=URIRef(study_uri) or str(evidence.value(source,U.measurementStatus))!='active' or str(evidence.value(source,U.outcomeStatus))!='reported':raise ValueError('Finding is absent, inactive or not reported')
  if not list(evidence.objects(source,PROV.wasDerivedFrom)) or not list(evidence.objects(source,U.sourceLocator)):raise ValueError('Finding provenance missing')
  for predicate,kind in [(F.indicator,U.Indicator),(F.state,U.State)]:
   concept=proposal.value(m,predicate)
   if (concept,RDF.type,kind) not in taxonomy:raise ValueError('Unknown indicator/State')
  if (proposal.value(m,F.indicator),U.measures,proposal.value(m,F.state)) not in taxonomy:raise ValueError('Missing measured-State link')
  import json
  comparison=json.loads(str(evidence.value(source,U.comparison) or '{}'))
  if comparison.get('kind') not in ('between-groups','association'):raise ValueError('Descriptive or within-group finding cannot be an intervention comparison')
  selection=list(proposal.objects(m,F.selected))
  if len(selection)!=1 or selection[0] not in (Literal(True),Literal(False)):raise ValueError('Explicit reviewed selection required')
  if selection[0]==Literal(False) and not str(proposal.value(m,A.selectionReason) or '').strip():raise ValueError('Unselected alternative requires a selection reason')
  orientation=str(proposal.value(m,F.orientation) or 'forward')
  if orientation not in ('forward','reverse'):raise ValueError('Unsupported orientation')
  estimand=str(proposal.value(m,F.estimand) or 'pairwise')
  if estimand not in ('pairwise','conditional-effect','marginal-effect','difference-in-differences','interaction','association'):raise ValueError('Unsupported estimand')
  unit=str(evidence.value(source,U.unit) or '')
  if unit in ('relative odds ratio','ratio of odds ratios') and selection[0]==Literal(True) and estimand=='pairwise':raise ValueError('Ratio of odds ratios requires an explicit model estimand')
  if estimand!='pairwise' and not str(proposal.value(m,F.model_context) or '').strip():raise ValueError('Model estimand requires reviewed context')
  if (estimand in ('interaction','association') or comparison.get('kind')=='association') and selection[0]==Literal(True):raise ValueError('Interaction/association is retained as an alternate, not a direct intervention effect')
 for component in proposal.objects(None,P.intervention):
  concepts=list(proposal.objects(component,U.taxonomyConcept)) or [component]
  if len(concepts)!=1 or (concepts[0],RDF.type,U.Intervention) not in taxonomy:raise ValueError('Unknown intervention concept')
 g=proposal+registry+interpretation
 rows=execute(g,{},evidence)
 if any(not r['numerically_usable'] or len(r['groups'])!=2 or not all(x['complete'] for x in r['groups']) for r in rows):raise ValueError('Usable comparison with complete groups required')
 return rows

def merge(current,proposal):
 for subject in set(proposal.subjects()):
  before=set(current.triples((subject,None,None)));after=set(proposal.triples((subject,None,None)))
  if before and before!=after:raise ValueError('Conflicting semantic definition; reviewed migration required')
 return current+proposal

@lru_cache(maxsize=4)
def _load(signature):
 graph=Graph()
 for path,_,_ in signature:graph.parse(path)
 index={}
 for m in graph.subjects(RDF.type,R.ResultMapping):
  sm=graph.value(m,F.study_mapping);study=str(graph.value(sm,F.study))
  index.setdefault(study,Graph())
  index[study]+=closure(graph,m)
 return graph,index

def indexed(paths):
 return _load(tuple((str(Path(p).resolve()),Path(p).stat().st_mtime_ns,Path(p).stat().st_size) for p in paths))

def annotate(rows,local_graph,config,taxonomy):
 """Add value-free comparison metadata only to eligible returned findings."""
 if not config.get('report_semantics'):return []
 g,index=indexed([*config['report_semantics'],config['source_registry'],config['result_interpretation'],config['aggregation_rules']])
 for node,key,expected in [(A.stuntingPreview,A.poolingEnabled,'false'),(A.stuntingPreview,A.confidence,'not-assessed'),(A.stuntingPreview,A.countUnit,'distinct-study'),(A.reportedStandardError,A.operation,'retain-per-estimate'),(A.reportedStandardError,A.significance,'not-inferred-from-SE-alone')]:
  if str(g.value(node,key))!=expected:raise ValueError('Unsupported maintained aggregation policy')
 failures=[]
 by_study={}
 for row in rows:by_study.setdefault(row['study'],[]).append(row)
 for study,visible in by_study.items():
  if study not in index:continue
  # Execute only returned, eligibility-filtered findings; unrelated studies never run.
  subset=index[study]
  # Validate the complete reviewed context before attaching only returned findings.
  # State filtering must not invalidate a study-wide context checksum.
  evidence=indexed([config['evidence']])[0] if config.get('evidence') else local_graph
  if not list(subset.subjects(RDF.type,R.ResultMapping)):continue
  try:
   # Context guards cover the study's mapped findings, so use the eligible local
   # graph and fail closed when a reviewed finding disappears or changes.
   subset=subset+closure(g,R['interpretations/reportedStandardError'])
   resolved=execute(subset,{},evidence)
   for r in resolved:
    m=URIRef(r['mapping'])
    if not r['numerically_usable']:continue
    if not all(x['complete'] for x in r['groups']):continue
    def concepts(ids):return [str(g.value(URIRef(x),U.taxonomyConcept) or x) for x in ids]
    left,right=r['left_only'],r['right_only']
    reverse=str(g.value(m,F.orientation) or 'forward')=='reverse' or (not left and bool(right))
    groups=r['groups'][::-1] if reverse else r['groups']
    if reverse:left,right=right,left
    metadata={'selected':g.value(m,F.selected)==Literal(True),'effect_multiplier':-1 if reverse else 1,
     'estimand':str(g.value(m,F.estimand) or 'pairwise'), 'model_context':str(g.value(m,F.model_context) or ''), 'study':study,'measurement':r['source_result'],'selector':None,'indicator':r['indicator'],'unit':r['unit'],
     'tested':concepts(left),'comparator':concepts(right),'background':concepts(r['shared_components']),
     'pValueSelector':None,'contrast_id':r['comparison_id'],'package':len(left)>1,
     'group_labels':[str(g.value(URIRef(x['id']),F.description) or g.value(URIRef(x['id']),F.control_kind) or x['id']) for x in groups]}
    for row in visible:
     if row['measurement']==r['source_result']:row['summary_mapping']=metadata
  except (ValueError,KeyError,TypeError) as error:failures.append({'study':study,'stage':'report_semantics','reason':str(error)})
 return failures


def registry_identity(evidence,source,record_id):
 """Resolve only an explicitly verified canonical registry link; never a title match."""
 import json
 matches=set()
 for study,raw in evidence.subject_objects(U.records):
  for record in json.loads(str(raw)):
   proof=record.get('verification') or {}
   if record.get('source')==source and record.get('record_id')==record_id and proof.get('basis') and proof.get('source_url') and proof.get('checked_by'):
    matches.add(str(study))
 if len(matches)>1:raise ValueError('Ambiguous verified registry identity')
 return next(iter(matches), 'https://universalevidence.com/studies/'+source+'/'+record_id)


def discovery_candidates(config,state,intervention,taxonomy,interventions):
 """Discover maintained evidence by reviewed result attribution, independent of registry text."""
 import json
 from rdflib.namespace import SKOS
 def descendants(g,root):
  seen={URIRef(root)};pending=list(seen)
  while pending:
   for n in g.subjects(SKOS.broader,pending.pop()):
    if n not in seen:seen.add(n);pending.append(n)
  return seen
 states=descendants(taxonomy,state);wanted=descendants(interventions,intervention)
 g,index=indexed(config.get('report_semantics',[]))
 evidence,_=indexed([config['evidence']]);identities=set()
 for study,sub in index.items():
  for m in sub.subjects(RDF.type,R.ResultMapping):
   if sub.value(m,F.selected)!=Literal(True):continue
   indicator=sub.value(m,F.indicator)
   if sub.value(m,F.state) not in states and not any(x in states for x in taxonomy.objects(indicator,U.measures)):continue
   comp=sub.value(m,F.comparison);left=sub.value(comp,P.interventionGroup);right=sub.value(comp,P.comparatorGroup)
   l=set(sub.objects(left,P.intervention));r=set(sub.objects(right,P.intervention))
   tested=(r-l) if str(sub.value(m,F.orientation) or '')=='reverse' or not l-r else l-r
   concepts={sub.value(x,U.taxonomyConcept) or x for x in tested}
   if concepts & wanted:identities.add(study)
 rows=[]
 for study in sorted(identities):
  for record in json.loads(str(evidence.value(URIRef(study),U.records) or '[]')):
   if not record.get('verification'):continue
   source=record['source'];sid=record['record_id']
   rows.append({'source':{'ctgov':'CT.gov','aea':'AEA','isrctn':'ISRCTN','who-ictrp':'WHO ICTRP'}.get(source,source),'study_id':sid,'url':record.get('url'),'status':None,'discovery':'reviewed-result-attribution'})
 return rows
