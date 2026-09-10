"""Request timing and observed background cleanup for query transports."""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
import logging
import time

logger = logging.getLogger("uvicorn.error.query_timing")
REQUEST_DEADLINE: ContextVar[float | None] = ContextVar('query_request_deadline', default=None)
TRACE: ContextVar[tuple[str, float] | None] = ContextVar('query_trace', default=None)
BACKGROUND_TASKS: set[asyncio.Task] = set()


def stage(name: str, **fields) -> None:
    trace = TRACE.get()
    if trace is not None:
        logger.info('query_stage request_id=%s elapsed_ms=%.3f stage=%s fields=%s',
                    trace[0], (time.monotonic() - trace[1]) * 1000, name, fields)


def observe(task: asyncio.Task, *, cancel: bool = False) -> None:
    """Own cleanup until it really finishes; never release a worker's permits here."""
    BACKGROUND_TASKS.add(task)
    if cancel and not task.done():
        task.cancel()

    def retire(completed):
        BACKGROUND_TASKS.discard(completed)
        try:
            completed.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.warning('Query background cleanup failed', exc_info=True)
        stage('cleanup_finished', task=completed.get_name())

    task.add_done_callback(retire)


async def bounded(awaitable, seconds: float):
    """A wall-clock bound that doesn't wait for cancellation-resistant cleanup."""
    task = asyncio.ensure_future(awaitable)
    try:
        done, _ = await asyncio.wait({task}, timeout=max(0, seconds))
        if not done:
            raise asyncio.TimeoutError
        return task.result()
    finally:
        if not task.done():
            observe(task, cancel=True)


async def timed_thread(function, *args):
    submitted = time.monotonic()
    name = getattr(function, '__name__', type(function).__name__)
    stage('thread_submit', function=name)

    def run():
        stage('thread_start', function=name, queue_ms=round((time.monotonic() - submitted) * 1000, 3))
        try:
            return function(*args)
        finally:
            stage('thread_end', function=name)

    return await asyncio.to_thread(run)
