"""Build a new immutable serving graph from reviewed, consumer-validated rows.
Never edits authoring evidence or publishes to a server. See compact-results.md.
"""
import argparse,json,hashlib
from datetime import datetime,timezone
from pathlib import Path
from rdflib import Graph,Namespace,URIRef,Literal,RDF
from rdflib.namespace import SKOS,XSD
C=Namespace('https://universalevidence.com/compact-results/')
STATE=URIRef('https://universalevidence.com/vocab/states/Stunting')
FIELDS=('measurement','study','indicator','indicator_label','value','unit','comparison','timing','uncertainty','subgroup','summary_mapping','citations','clear_direction','direction_sign','intervention_labels')

def build(data,taxonomy):
 g=Graph();g.bind('c',C);studies=set()
 for source in data['rows']:
  row={k:source[k] for k in FIELDS if k in source};mapping=row['summary_mapping']
  if mapping.get('selected') is False or not mapping.get('tested'):continue
  if mapping.get('estimand') in ('association','interaction'):continue
  if row['study'] not in data['studies']:raise ValueError('Missing verified study metadata')
  rid=URIRef(row['measurement']);sid=URIRef(row['study']);studies.add(str(sid))
  row['intervention_labels']={uri:str(taxonomy.value(URIRef(uri),SKOS.prefLabel) or uri.split('/')[-1]) for uri in mapping['tested']}
  for p,o in [(RDF.type,C.Result),(C.state,STATE),(C.study,sid),(C.indicator,URIRef(row['indicator'])),(C.value,Literal(float(row['value']))),(C.unit,Literal(row['unit'])),(C.clearDirection,Literal(row['clear_direction'])),(C.direction,Literal(row['direction_sign'])),(C.package,Literal(bool(mapping.get('package')))),(C.row,Literal(json.dumps(row,separators=(',',':'))))]:g.add((rid,p,o))
  for uri in mapping['tested']:
   g.add((rid,C.intervention,URIRef(uri)))
   pending=[URIRef(uri)];seen=set()
   while pending:
    node=pending.pop()
    if node in seen:continue
    seen.add(node);g.add((rid,C.matchesIntervention,node));pending.extend(taxonomy.objects(node,SKOS.broader))
  for uri in mapping.get('comparator',[]):g.add((rid,C.comparator,URIRef(uri)))
  for citation in row.get('citations',[]):
   if citation.get('url','').startswith('https://'):g.add((rid,C.source,URIRef(citation['url'])))
 for sid in studies:
  metadata=data['studies'][sid];g.add((URIRef(sid),C.metadata,Literal(json.dumps(metadata,separators=(',',':')))))
  for r in metadata.get('registry_records',[]):g.add((URIRef(sid),C.registryIdentifier,Literal(r['source']+':'+r['record_id'])))
 return g

if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('input');p.add_argument('taxonomy');p.add_argument('output');a=p.parse_args()
 data=json.loads(Path(a.input).read_text());g=build(data,Graph().parse(a.taxonomy));g.serialize(a.output,format='turtle')
 digest=hashlib.sha256(Path(a.output).read_bytes()).hexdigest()
 manifest={'graph':'https://universalevidence.com/graph/compact-results/stunting-'+digest[:16],'sha256':digest,'triples':len(g),'results':len(set(g.subjects(RDF.type,C.Result))),'studies':len(set(g.objects(None,C.study))),'created_at':datetime.now(timezone.utc).isoformat(),'capture_failures':data.get('capture_failures',[]),'scope':'Reviewed Stunting serving snapshot; not an exhaustive evidence search'}
 Path(a.output+'.manifest.json').write_text(json.dumps(manifest,indent=2));print(json.dumps({k:v for k,v in manifest.items() if k!='capture_failures'}))
