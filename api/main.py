"""FastAPI transport for Universal Evidence queries."""

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
import sys

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from api.query_v2_timing import QueryResponseTimingMiddleware
from api.routes.graph import router as graph_router
from api.routes.graph_v2 import router as graph_v2_router
from api.routes.query import router as query_router
from api.routes.query_v2 import router as query_v2_router
from api.routes.taxonomy import router as taxonomy_router
from api.routes.taxonomy import prewarm_trees
from api.routes.vocab import router as vocab_router

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        from query_interventions import _load_mesh_state_map, _load_mesh_intervention_map, _load_aea_outcome_concept_map, _load_fuseki_xwalk
        await asyncio.to_thread(_load_mesh_state_map)
        await asyncio.to_thread(_load_mesh_intervention_map)
        logger.info("MeSH→State map pre-warmed")
        logger.info("MeSH→Intervention map pre-warmed")
        await asyncio.to_thread(_load_aea_outcome_concept_map)
        logger.info("AEA outcome concept map pre-warmed")
        await asyncio.to_thread(_load_fuseki_xwalk)
        logger.info("Fuseki crosswalk cache pre-warmed")
    except Exception:
        logger.warning("MeSH→State map pre-warm failed (Fuseki may not be ready yet)", exc_info=True)
    try:
        await prewarm_trees()
    except Exception:
        logger.warning("Taxonomy tree pre-warm failed", exc_info=True)
    # Search presentation depends on the exact R2 crosswalk snapshot loaded in
    # Fuseki. Make this a readiness requirement so a cold request never spends
    # its five-second source budget scanning the taxonomy or silently files a
    # mapped study under "Other studies."
    from query_v2 import (
        prewarm_direct_intervention_crosswalks,
        prewarm_graph_direct_state_crosswalks,
    )
    await asyncio.to_thread(prewarm_direct_intervention_crosswalks)
    # Graph attribution must not discover its source-scoped State maps inside
    # the six-second request deadline: synchronous Fuseki work cannot be
    # cancelled once delegated to a worker thread.
    await asyncio.to_thread(prewarm_graph_direct_state_crosswalks)
    from api.cache_warming import warm_common_queries
    cache_warm_task = asyncio.create_task(warm_common_queries())
    yield
    cache_warm_task.cancel()
    try:
        await cache_warm_task
    except asyncio.CancelledError:
        pass


app = FastAPI(title="Universal Evidence API", lifespan=lifespan)
app.add_middleware(QueryResponseTimingMiddleware)


@app.get("/health", include_in_schema=False)
async def health() -> dict[str, str]:
    """Report that the API process is accepting requests."""
    return {"status": "ok"}

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://universalevidence.com",
        "https://www.universalevidence.com",
        "http://localhost:5173",
        "http://localhost:3000",
    ],
    allow_methods=["GET"],
    allow_headers=["*"],
)

app.include_router(query_router)
app.include_router(query_v2_router)
app.include_router(taxonomy_router)
app.include_router(vocab_router)
app.include_router(graph_router)
app.include_router(graph_v2_router)
