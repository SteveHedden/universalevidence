"""Opt-in local review UI/API. Run explicitly; never mounted by production main."""
import asyncio
import json
import os
from pathlib import Path
import sys
import time
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, Response
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from search_results import search_results, render
from result_config import load_result_config

app=FastAPI(title='UE local search integration review')
_slots=asyncio.Semaphore(2)

@app.get('/',response_class=HTMLResponse)
def home():
    return '''<!doctype html><meta charset="utf-8"><title>UE local result search</title>
    <h1>UE local result search</h1><p>Reviewed local mappings; fresh source results. Completed studies only.</p>
    <form action="query/results"><p>State URI <input name="state" size="90" required value="https://universalevidence.com/vocab/states/Stunting"></p>
    <p>Intervention URI <input name="intervention" size="90" required value="https://universalevidence.com/vocab/interventions/NutritionEducation"></p>
    <input type="hidden" name="format" value="html"><button>Search and retrieve results</button></form>'''

@app.get('/query/results')
async def results(state:str, intervention:str, format:str=Query('json',pattern='^(json|html|turtle)$')):
    started=time.perf_counter()
    if not state.startswith('https://universalevidence.com/vocab/states/') or not intervention.startswith('https://universalevidence.com/vocab/interventions/'):
        raise HTTPException(422,'Canonical State and Intervention URIs required')
    path=os.getenv('UE_RESULTS_CONFIG')
    if not path: raise HTTPException(503,'Explicit reviewed UE_RESULTS_CONFIG is required')
    config=load_result_config(path)
    if not config.get('review_only'): raise HTTPException(503,'Only local review configuration is supported')
    if _slots.locked(): raise HTTPException(429,'Local search capacity reached')
    async with _slots:
        result=await search_results(state,intervention,config)
    result['timings']['request_seconds_before_encoding']=time.perf_counter()-started
    if format=='html': body=render(result); kind='text/html'
    elif format=='turtle':body=result['rdf'];kind='text/turtle'
    else:body=json.dumps(result,ensure_ascii=False,allow_nan=False);kind='application/json'
    return Response(body,media_type=kind,headers={'Cache-Control':'no-store','Server-Timing':f'total;dur={(time.perf_counter()-started)*1000:.3f}'})
