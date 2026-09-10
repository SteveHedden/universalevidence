"""Measure the actual ASGI response start/end, including route serialization."""
import logging
import time

logger = logging.getLogger('uvicorn.error.query_timing')


class QueryResponseTimingMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or scope.get('path') != '/query/v2':
            return await self.app(scope, receive, send)
        started = time.monotonic()
        scope['query_started_at'] = started

        async def timed_send(message):
            await send(message)
            if message['type'] == 'http.response.start' or (
                message['type'] == 'http.response.body' and not message.get('more_body', False)
            ):
                logger.info('query_stage request_id=%s elapsed_ms=%.3f stage=%s status=%s',
                            scope.get('query_request_id', 'validation'),
                            (time.monotonic() - started) * 1000,
                            'response_start' if message['type'] == 'http.response.start' else 'response_end',
                            message.get('status', '-'))

        await self.app(scope, receive, timed_send)
