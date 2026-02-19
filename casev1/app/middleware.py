import logging
import time

logger = logging.getLogger("exchange.slow_requests")

SLOW_REQUEST_THRESHOLD_MS = 100


class SlowRequestMiddleware:
    """Pure ASGI middleware — avoids the overhead of Starlette's BaseHTTPMiddleware."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        start = time.perf_counter()
        status_code = 0

        async def send_wrapper(message):
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
            await send(message)

        await self.app(scope, receive, send_wrapper)

        elapsed_ms = (time.perf_counter() - start) * 1000
        if elapsed_ms > SLOW_REQUEST_THRESHOLD_MS:
            logger.warning(
                "SLOW REQUEST: %s %s took %.1fms (status %d)",
                scope["method"],
                scope["path"],
                elapsed_ms,
                status_code,
            )
