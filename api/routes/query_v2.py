"""Additive GET /query/v2 route; legacy search remains untouched."""

from __future__ import annotations

import asyncio
import json
import time
from uuid import uuid4
from pathlib import Path
import sys
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from starlette.responses import Response
from api.vocabulary_compat import canonical_vocabulary_uri


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from query_interventions import (  # noqa: E402
    ClientDisconnectedError,
    await_query_or_disconnect,
)
from query_v2 import AXES, canonical_query, execute_query_v2, RESPONSE_TIMEOUT_SECONDS, ExecutionProgress  # noqa: E402
from api.query_v2_cache import query_v2_cache  # noqa: E402


from query_runtime import REQUEST_DEADLINE, TRACE, bounded, stage

router = APIRouter()


def encode_response(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")

VOCAB_PREFIX = "https://universalevidence.com/vocab/"
AXIS_PREFIXES: dict[str, tuple[str, ...]] = {
    "state": (VOCAB_PREFIX,),
    "condition": (VOCAB_PREFIX,),
    "intervention": (f"{VOCAB_PREFIX}interventions/",),
    "outcome": (VOCAB_PREFIX,),
    "region": (f"{VOCAB_PREFIX}regions/", "https://sws.geonames.org/"),
}


def _validated_values(axis: str, values: list[str]) -> list[str]:
    cleaned: list[str] = []
    for raw in values:
        value = raw.strip()
        if not value:
            raise HTTPException(status_code=422, detail=f"{axis} values cannot be blank")
        if not value.startswith("https://") or not value.startswith(AXIS_PREFIXES[axis]):
            raise HTTPException(
                status_code=422,
                detail=f"{axis} must be a canonical HTTPS taxonomy URI",
            )
        cleaned.append(canonical_vocabulary_uri(value))
    return cleaned


def build_query_v2_request(
    *,
    condition: list[str],
    intervention: list[str],
    outcome: list[str],
    region: list[str],
    condition_logic: str,
    intervention_logic: str,
    outcome_logic: str,
    region_logic: str,
    state: list[str] | None = None,
    state_logic: str = "or",
):
    values = {
        "state": _validated_values("state", state or []),
        "condition": _validated_values("condition", condition),
        "intervention": _validated_values("intervention", intervention),
        "outcome": _validated_values("outcome", outcome),
        "region": _validated_values("region", region),
    }
    if not any(values.values()):
        raise HTTPException(status_code=422, detail="Provide at least one query axis")
    try:
        return canonical_query(
            values,
            {
                "state": state_logic,
                "condition": condition_logic,
                "intervention": intervention_logic,
                "outcome": outcome_logic,
                "region": region_logic,
            },
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/query/v2")
async def query_v2(
    request: Request,
    state: list[str] = Query(default=[]),
    condition: list[str] = Query(default=[]),
    intervention: list[str] = Query(default=[]),
    outcome: list[str] = Query(default=[]),
    region: list[str] = Query(default=[]),
    state_logic: Literal["or", "and"] = "or",
    condition_logic: Literal["or", "and"] = "or",
    intervention_logic: Literal["or", "and"] = "or",
    outcome_logic: Literal["or", "and"] = "or",
    region_logic: Literal["or", "and"] = "or",
) -> Response:
    """Execute the frozen query-v2 contract without changing v1 traffic."""
    started = request.scope.get("query_started_at", time.monotonic())
    query = build_query_v2_request(
        state=state,
        condition=condition,
        intervention=intervention,
        outcome=outcome,
        region=region,
        state_logic=state_logic,
        condition_logic=condition_logic,
        intervention_logic=intervention_logic,
        outcome_logic=outcome_logic,
        region_logic=region_logic,
    )
    # Reserve time for encoding/response start; this is one total caller budget.
    response_deadline = started + RESPONSE_TIMEOUT_SECONDS
    reserve = min(0.2, RESPONSE_TIMEOUT_SECONDS * 0.1)
    deadline_token = REQUEST_DEADLINE.set(response_deadline - reserve)
    request_id = uuid4().hex
    request.scope["query_request_id"] = request_id
    trace_token = TRACE.set((request_id, started))
    try:
        stage("request_start", budget_seconds=RESPONSE_TIMEOUT_SECONDS)
        payload = await await_query_or_disconnect(
            query_v2_cache.get_or_execute(query, execute_query_v2), request
        )
        stage("serialization_start")
        try:
            body = await bounded(asyncio.to_thread(encode_response, payload),
                                 response_deadline - time.monotonic() - min(0.02, reserve / 4))
        except asyncio.TimeoutError:
            # Distinguish response-preparation failure from an empty evidence set.
            fallback = ExecutionProgress().snapshot()
            fallback["meta"]["response_timeout_stage"] = "serialization"
            fallback["meta"]["execution_status"] = "timeout"
            fallback["meta"]["sources"] = {source: {**meta, "returned_unique_studies": 0}
                                              for source, meta in payload["meta"]["sources"].items()}
            fallback["meta"]["completed_sources"] = payload["meta"].get("completed_sources", [])
            fallback["meta"]["timed_out_sources"] = payload["meta"].get("timed_out_sources", [])
            body = json.dumps(fallback, separators=(",", ":")).encode()
        stage("serialization_end", bytes=len(body))
        stage("response_ready", status=200)
        return Response(body, media_type="application/json", headers={"X-Request-ID": request_id})
    except ClientDisconnectedError as exc:
        stage("client_disconnected")
        raise HTTPException(status_code=499, detail="Client disconnected") from exc
    finally:
        REQUEST_DEADLINE.reset(deadline_token)
        TRACE.reset(trace_token)


@router.get("/query/v2/cache/stats")
def query_v2_cache_stats() -> dict[str, int | str]:
    return query_v2_cache.stats()
