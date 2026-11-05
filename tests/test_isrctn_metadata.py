import asyncio
from copy import deepcopy
import json
from pathlib import Path
import xml.etree.ElementTree as ET
import httpx
import pytest
from scripts.fetch_isrctn import parse_isrctn_xml
from scripts import query_v2 as q
from scripts.query_interventions import format_result
from api.routes.query import normalize_query_results
FIXTURES=Path(__file__).parent/'fixtures/isrctn_metadata'

def parse(body):return parse_isrctn_xml('<trial><isrctn>12345678</isrctn>'+body+'</trial>')[0]

@pytest.mark.parametrize('sid,count,roles',[('ISRCTN30295544',8,(1,7)),('ISRCTN18275142',10,(1,9))])
def test_observed_nested_outcome_loss(sid,count,roles):
 raw=(FIXTURES/(sid+'.xml')).read_bytes();row=parse_isrctn_xml(raw)[0]
 specs=row['outcome_specifications'];assert len(specs)==len(row['outcomes'])==count
 assert tuple(sum(s['role']==role for s in specs) for role in ('primary','secondary'))==roles
 assert all(s['variable'] and s['method'] and s['timepoints'] and s['source_id'] for s in specs)
 assert all(o['type']=='pre-specified-measure' and 'value' not in o for o in row['outcomes'])
 assert row['result_availability']['structured_numerical_results']=='not_extracted'

def test_legacy_output_preserved_and_empty_ignored():
 row=parse('<primaryOutcome>HAZ at 18 months</primaryOutcome><secondaryOutcome>Weight at 12 months</secondaryOutcome><outcomeMeasure> </outcomeMeasure>')
 assert row['outcomes']==[{'type':'pre-specified-measure','measure':'HAZ at 18 months'},{'type':'pre-specified-measure','measure':'Weight at 12 months'}]
 assert [s['role'] for s in row['outcome_specifications']]==['primary','secondary']

def test_namespace_wrappers_fields_and_stable_fallback_ids():
 body='<outcomeMeasures><primaryOutcomes><outcomeMeasure><variable>HAZ</variable><method>WHO</method><timepoints>18 months</timepoints></outcomeMeasure></primaryOutcomes></outcomeMeasures>'
 a=parse(body);b=parse_isrctn_xml('<trial xmlns="urn:other"><isrctn>12345678</isrctn>'+body+'</trial>')[0]
 assert len(a['outcome_specifications'])==len(a['outcomes'])==1
 assert a['outcome_specifications'][0]['id']==b['outcome_specifications'][0]['id']
 assert a['outcome_specifications'][0]['provenance'][0]['xml_path'].endswith('/primaryOutcomes[1]/outcomeMeasure[1]')
 changed=parse(body.replace('18 months','24 months'))
 assert changed['outcome_specifications'][0]['id']!=a['outcome_specifications'][0]['id']

def test_coexistence_dedup_keeps_history_and_context():
 row=parse('<primaryOutcomes><outcomeMeasure id="one"><variable>HAZ</variable><method>WHO</method><timepoints>18m</timepoints></outcomeMeasure></primaryOutcomes><primaryOutcome> HAZ </primaryOutcome><secondaryOutcome>Historical weight at 12 months</secondaryOutcome>')
 assert len(row['outcome_specifications'])==2
 assert row['outcome_specifications'][0]['legacy_representations'][0]['text']=='HAZ'
 assert row['outcome_specifications'][1]['legacy_text']=='Historical weight at 12 months'
 # Same variable with differing method/timepoint is never merged.
 row=parse('<primaryOutcomes><outcomeMeasure id="one"><variable>HAZ</variable><method>WHO</method></outcomeMeasure><outcomeMeasure id="two"><variable>HAZ</variable><method>CDC</method></outcomeMeasure></primaryOutcomes><primaryOutcome>HAZ</primaryOutcome>')
 assert len(row['outcome_specifications'])==3
 assert len(q.dedupe_presentation([row,row])[0]['outcomes'])==3

def test_empty_and_method_only_keep_specification_not_fake_measure():
 row=parse('<secondaryOutcomes><outcomeMeasure id="empty"/><outcomeMeasure id="method"><method>WHO anthropometry</method></outcomeMeasure></secondaryOutcomes>')
 assert len(row['outcome_specifications'])==1 and row['outcomes']==[]
 assert row['outcome_specifications'][0]['variable'] is None

@pytest.mark.parametrize('sid,outputs',[('ISRCTN03467700',2),('ISRCTN10612149',6)])
def test_results_links_and_files_are_not_estimates(sid,outputs):
 row=parse_isrctn_xml((FIXTURES/(sid+'.xml')).read_bytes())[0]
 meta=row['report_metadata'];assert len(meta['outputs'])==outputs
 assert row['result_availability']['results_documents']=='typed_metadata_present'
 assert row['result_availability']['structured_numerical_results']=='not_extracted'
 assert any(o['attributes']['outputType']=='resultsarticle' and o['external_links'] for o in meta['outputs'])
 if sid=='ISRCTN10612149':
  pdfs=[o for o in meta['outputs'] if o['local_files']]
  assert {'basicresults','plainenglishresults'} <= {o['attributes']['outputType'] for o in pdfs}
  for o in pdfs:
   attrs=o['local_files'][0]['attributes'];assert attrs['mimeType']=='application/pdf' and attrs['md5sum'] and attrs['fileId']
   assert 'url' not in o['local_files'][0]

def test_allowlist_privacy_repeat_outputs_and_stage_only():
 row=parse('<results><publicationStage>Results</publicationStage><internalNote>secret</internalNote></results><outputs>'+('<output id="o" outputType="basicresults" artefactType="LocalFile" peerReviewed="false" patientFacing="true" createdBy="secret"><localFile fileId="f" mimeType="application/pdf" version="" sha256sum="abc" internalNote="secret"/><productionNotes>secret</productionNotes></output>'*2)+'</outputs>')
 assert len(row['report_metadata']['outputs'])==2 and 'secret' not in json.dumps(row)
 assert row['report_metadata']['outputs'][0]['local_files'][0]['attributes']['sha256sum']=='abc'
 only=parse('<results><publicationStage>Results</publicationStage></results>')
 assert only['result_availability']['results_documents']=='not_identified'
 assert only['result_availability']['structured_numerical_results']=='not_extracted'

def test_duplicate_ids_and_provenance_are_not_silently_collapsed():
 x='<outcomeMeasure id="same"><variable>HAZ</variable><timepoints>{}</timepoints></outcomeMeasure>'
 row=parse('<primaryOutcomes>'+x.format('12m')+x.format('24m')+x.format('12m')+'</primaryOutcomes>')
 specs=row['outcome_specifications'];assert len(specs)==2 and len({s['id'] for s in specs})==2
 assert len(specs[0]['provenance'])==2

def test_all_public_paths_keep_additive_fields_and_other_sources_unchanged():
 row=parse_isrctn_xml((FIXTURES/'ISRCTN10612149.xml').read_bytes())[0]
 stamped=q._stamp_isrctn([row],{},{});search=q.dedupe_presentation(stamped)[0]
 for public in (format_result(row),normalize_query_results([row])[0],search):
  for key in ('outcome_specifications','report_metadata','result_availability'):assert public[key]==row[key]
 token=q._GRAPH_ATTRIBUTION.set(True)
 try:assert q.dedupe_presentation(stamped)[0]['report_metadata']==row['report_metadata']
 finally:q._GRAPH_ATTRIBUTION.reset(token)
 assert 'report_metadata' not in format_result({'source':'AEA','study_id':'other'})

def test_http_xml_to_v2_response_and_contract(monkeypatch):
 from fastapi import FastAPI
 from fastapi.testclient import TestClient
 from api.routes import query_v2 as route
 raw=(FIXTURES/'ISRCTN30295544.xml').read_bytes()
 async def pipeline(query,execute):
  async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request:httpx.Response(200,content=raw))) as client:
   rows=await q._fetch_isrctn_batch(client,'https://example.test/query','test',1)
  results=q.dedupe_presentation(q._stamp_isrctn(rows,{},{}))
  return {'results':results,'meta':{'api_version':'query-v2','returned_unique_studies':1,'limit_per_source_branch':100,'truncated':False,'approximate':False,'sources':{}}}
 monkeypatch.setattr(q,'_isrctn_country_norm',lambda:{})
 monkeypatch.setattr(route.query_v2_cache,'get_or_execute',pipeline)
 app=FastAPI();app.include_router(route.router)
 response=TestClient(app).get('/query/v2',params={'state':'https://universalevidence.com/vocab/states/Stunting'})
 assert response.status_code==200
 payload=response.json();assert len(payload['results'][0]['outcome_specifications'])==8
 schema=json.loads((Path(__file__).parents[1]/'contracts/query-v2/response.schema.json').read_text())
 for field in ('outcome_specifications','report_metadata','result_availability'):
  assert field in schema['$defs']['studyResult']['properties'] and field in payload['results'][0]


def test_repeated_structured_entries_do_not_block_fallback_alias():
 item='<outcomeMeasure id="repeat"><variable>HAZ</variable><method>WHO</method></outcomeMeasure>'
 row=parse('<primaryOutcomes>'+item+item+'</primaryOutcomes><primaryOutcome>HAZ</primaryOutcome>')
 assert len(row['outcome_specifications'])==1
 spec=row['outcome_specifications'][0]
 assert len(spec['provenance'])==2 and spec['legacy_representations'][0]['text']=='HAZ'

@pytest.mark.parametrize('graph',[False,True])
def test_duplicate_search_rows_preserve_later_metadata(graph):
 first=parse('<primaryOutcome>HAZ</primaryOutcome>')
 second=parse('<secondaryOutcome>Stunting prevalence</secondaryOutcome><outputs><output outputType="resultsarticle"><externalLink url="https://example.org/results"/></output></outputs>')
 token=q._GRAPH_ATTRIBUTION.set(graph)
 try:
  rows=q.dedupe_presentation(q._stamp_isrctn([first,second],{},{}))
  assert len(rows)==1
  assert len(rows[0]['outcome_specifications'])==2
  assert len(rows[0]['report_metadata']['outputs'])==1
  assert rows[0]['result_availability']['planned_specification_count']==2
  assert rows[0]['result_availability']['results_documents']=='typed_metadata_present'
  assert rows[0]['result_availability']['structured_numerical_results']=='not_extracted'
 finally:q._GRAPH_ATTRIBUTION.reset(token)
