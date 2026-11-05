"""Independent live IATI endpoint; does not change research query contracts."""
import asyncio
from functools import lru_cache
from pathlib import Path
import os
from fastapi import APIRouter, HTTPException, Query, Response
from scripts.iati_live import Organizations
from scripts.iati_live_rules import RuleBook
from scripts.iati_search import ProjectSearch
from scripts.study_projects import lookup as reviewed_projects

router=APIRouter()

@lru_cache(maxsize=1)
def service():
    class SearchService:
        def __init__(self):
            self.projects=ProjectSearch()
            self.legacy=Organizations(RuleBook(os.getenv('UE_IATI_RULES') or None))
        async def lookup(self,state='',intervention='',context=''):
            # Retain the separately reviewed cash-transfer contract unchanged.
            if intervention=='https://universalevidence.com/vocab/interventions/UnconditionalCashTransfer':
                return await self.legacy.lookup(state,intervention,context)
            return await self.projects.lookup(state,intervention,context)
    return SearchService()

@router.get('/query/organizations')
async def organizations(response: Response, state: str=Query('',max_length=512), intervention: str=Query('',max_length=512), context: str=Query('',max_length=4096)):
    for value,axis in [(state,'states'),(intervention,'interventions')]:
        if value and not value.startswith(f'https://universalevidence.com/vocab/{axis}/'):
            raise HTTPException(422,'Use a canonical State or Intervention URI')
    response.headers['Cache-Control']='no-store'
    async def local_lookup():
        try:
            async with asyncio.timeout(8):
                return await asyncio.to_thread(reviewed_projects, state, intervention, context)
        except Exception:
            return {'status':'unavailable','projects':[],'count':0,'errors':['reviewed-snapshot-unavailable'],
                    'scope':'Reviewed local project metadata','freshness':'Current operation is not freshly verified.'}

    async def iati_lookup():
        try:
            engine = service()
            async with asyncio.timeout(8):
                return await engine.lookup(state,intervention,context)
        except Exception as exc:
            # Both sources run inside the existing endpoint budget. Failure cannot
            # erase successful metadata from the independent source.
            return {'status':'error','mode':'project-mentions','projects':[], 'organizations':[],
                    'unresolved_participants':[], 'missing_dimensions':[], 'coverage':{'truncated':True},
                    'retrieved_at':'Unavailable', 'candidate_mapping_review':False,
                    'errors':['endpoint-deadline' if isinstance(exc, TimeoutError) else 'iati-unavailable']}
    local, result = await asyncio.gather(local_lookup(), iati_lookup())
    return {**result, 'study_projects': local}
