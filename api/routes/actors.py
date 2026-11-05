"""Read-only bounded activity/organization lookup; separate from research search."""
from fastapi import APIRouter, HTTPException, Query, Response
from scripts import iati_actors as store

router=APIRouter(prefix='/actors',tags=['actors'])

def read(operation):
    try:
        with store.reader() as conn:return operation(conn)
    except store.Conflict as exc:raise HTTPException(409,str(exc)) from exc
    except (OSError,store.StoreError) as exc:raise HTTPException(503,'Actor population unavailable') from exc
    except ValueError as exc:raise HTTPException(422,str(exc)) from exc
    except KeyError as exc:raise HTTPException(404,'Activity not found') from exc

@router.get('/organizations')
def organizations(state: str | None=None, intervention: str | None=None, role: str | None=None,
                  limit: int=Query(25,ge=1,le=100), offset: int=Query(0,ge=0), snapshot: str | None=None):
    return read(lambda conn:store.lookup(conn,state=state,intervention=intervention,role=role,limit=limit,offset=offset,snapshot=snapshot))

@router.get('/unresolved-participants')
def unresolved(state: str | None=None, intervention: str | None=None, role: str | None=None,
               limit: int=Query(25,ge=1,le=100), offset: int=Query(0,ge=0), snapshot: str | None=None):
    return read(lambda conn:store.lookup(conn,state=state,intervention=intervention,role=role,limit=limit,offset=offset,snapshot=snapshot,unresolved=True))

@router.get('/activities/detail')
def activity(id: str, history: bool=False):return read(lambda conn:store.detail(conn,id,history))

@router.get('/vocabulary')
def vocabulary():return read(lambda conn:Response(store.export_agents(conn).serialize(format='turtle'),media_type='text/turtle'))
