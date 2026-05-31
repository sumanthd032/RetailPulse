"""Request middleware — trace_id injection, structured JSON logging, timing."""

from __future__ import annotations

import json
import logging
import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger("api.request")


class StructuredLoggingMiddleware(BaseHTTPMiddleware):
    """Injects a trace_id per request and logs structured JSON on completion."""

    async def dispatch(self, request: Request, call_next) -> Response:
        trace_id = str(uuid.uuid4())
        request.state.trace_id = trace_id

        start = time.monotonic()
        response = await call_next(request)
        latency_ms = round((time.monotonic() - start) * 1000, 2)

        # Extract store_id from path if present (/stores/{store_id}/...)
        path_parts = request.url.path.strip("/").split("/")
        store_id = None
        if len(path_parts) >= 2 and path_parts[0] == "stores":
            store_id = path_parts[1]

        log_record = {
            "trace_id": trace_id,
            "method": request.method,
            "path": request.url.path,
            "store_id": store_id,
            "status_code": response.status_code,
            "latency_ms": latency_ms,
        }
        logger.info(json.dumps(log_record))

        response.headers["X-Trace-Id"] = trace_id
        return response
