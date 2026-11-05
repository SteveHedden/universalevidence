"""Reviewed API comparison attribution. Values stay in transient source responses."""
from functools import lru_cache
from pathlib import Path
from rdflib import RDF, URIRef
try:
 from .ctgov_result_rules import load_rules
 from .ctgov_results import digest, UE
except ImportError:
 from ctgov_result_rules import load_rules
 from ctgov_results import digest, UE

FIELDS={'key','locator','rule_sha256','context_sha256','groups','multiplier','selected','timepoint','reason'}

def identity(entry):return (entry['key'],entry['locator'])

def validate(entry,record,taxonomy):
 if set(entry)!=FIELDS or entry['selected'] is not True:raise ValueError('Explicit exact attribution contract and selection required')
 cfg=record.get('mapping')
 if not cfg or not record.get('reported') or not record.get('numerically_usable'):raise ValueError('No usable maintained API comparison')
 if record['kind'] not in ('source_comparison','derived_comparison'):raise ValueError('Descriptive records cannot be effect summaries')
 if entry['key']!=cfg['key'] or entry['locator']!=record['locator'] or entry['context_sha256']!=cfg['context_sha256'] or entry['rule_sha256']!=digest(record['comparison']):raise ValueError('Stale API attribution context or comparison rule')
 if record['comparison'].get('statistic') not in ('difference','ratio') or record['comparison'].get('scale')!='identity':raise ValueError('API summary requires a reviewed identity-scale difference or ratio')
 if type(entry['multiplier']) is not int or entry['multiplier'] not in (-1,1):raise ValueError('Invalid comparison orientation')
 if record['classification'].get('direction')=='unknown/unsupported':raise ValueError('Unsupported comparison direction')
 for field in ('timepoint','reason'):
  if not isinstance(entry[field],str) or not entry[field].strip():raise ValueError('Missing '+field)
 groups=entry['groups'];source=record['context'].get('groups',[])
 if not isinstance(groups,list) or len(groups)!=2 or len(source)!=2 or {g['source_id'] for g in groups}!={g['id'] for g in source}:raise ValueError('Exactly two matching source groups required')
 if record['kind']=='derived_comparison':
  expected=[g['id'] for g in source][::entry['multiplier']]
  if [g['source_id'] for g in groups]!=expected:raise ValueError('Derived operand orientation mismatch')
 components={};sets=[]
 for group in groups:
  if set(group)!={'source_id','label','components','complete'} or group['complete'] is not True or not group['label']:raise ValueError('Complete explicit groups required')
  ids=[]
  for component in group['components']:
   if set(component)!={'instance','concept','description'} or not component['instance'] or not component['description']:raise ValueError('Invalid assigned component')
   concept=component['concept']
   if concept is not None and (URIRef(concept),RDF.type,UE.Intervention) not in taxonomy:raise ValueError('Unknown intervention concept')
   if component['instance'] in ids:raise ValueError('Duplicate group component')
   if component['instance'] in components and components[component['instance']]!=component:raise ValueError('Shared component definitions disagree')
   components[component['instance']]=component;ids.append(component['instance'])
  sets.append(set(ids))
 left=sets[0]-sets[1];right=sets[1]-sets[0];shared=sets[0]&sets[1]
 if not left or any(components[i]['concept'] is None for i in left):raise ValueError('Tested components need reviewed taxonomy attribution')
 def concepts(ids):return sorted({components[i]['concept'] for i in ids if components[i]['concept']})
 return {'tested':concepts(left),'comparator':concepts(right),'background':concepts(shared),
         'package':len(left)>1,'group_labels':[g['label'] for g in groups],
         'effect_multiplier':entry['multiplier'],'summary_timepoint':entry['timepoint'],
         'contrast_id':digest({'key':entry['key'],'locator':entry['locator'],'groups':groups})}

@lru_cache(maxsize=4)
def _load(path,mtime,size):
 entries=load_rules(path)['outcomes'];index={}
 for e in entries:
  ident=identity(e)
  if ident in index:raise ValueError('Duplicate API attribution')
  index[ident]=e
 return index

def indexed(path):
 p=Path(path);s=p.stat();return _load(str(p.resolve()),s.st_mtime_ns,s.st_size)

def annotate(rows,results,path,taxonomy):
 """Join current adapter records to returned rows; no upstream requests or saved values."""
 index=indexed(path);visible={r['measurement']:r for r in rows};failures=[]
 for result in results:
  if isinstance(result,Exception):continue
  for response in result['responses']:
   sid=response.payload['protocolSection']['identificationModule']['nctId']
   for record in result['records']:
    if record['study']!=sid:continue
    mid=response.url+'#sha256-'+response.sha256+record['locator']
    row=visible.get(mid);cfg=record.get('mapping')
    if row is None or not cfg:continue
    entry=index.get((cfg['key'],record['locator']))
    if entry is None:continue
    try:
     metadata=validate(entry,record,taxonomy)
     row['summary_mapping']={**metadata,'study':row['study'],'measurement':mid,'selector':None,
      'indicator':row['indicator'],'unit':row['unit'],'pValueSelector':None}
    except (ValueError,KeyError,TypeError) as exc:
     failures.append({'study':sid,'stage':'api_semantics','reason':str(exc)})
 return failures
