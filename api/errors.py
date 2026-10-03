"""Centralized error handling — every non-2xx response is JSON with an
`error: string` field, matching the frontend's hard contract requirement
(src/services/api.ts throws on `errData.error`)."""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from pipeline.storage.supabase_store import SupabaseNotConfigured

logger = logging.getLogger("reelvault")


class ApiError(Exception):
    def __init__(self, status_code: int, message: str, code: str | None = None):
        self.status_code = status_code
        self.message = message
        self.code = code
        super().__init__(message)


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def handle_api_error(_: Request, exc: ApiError) -> JSONResponse:
        content = {"error": exc.message}
        if exc.code:
            content["code"] = exc.code
        return JSONResponse(status_code=exc.status_code, content=content)

    @app.exception_handler(SupabaseNotConfigured)
    async def handle_supabase_not_configured(_: Request, exc: SupabaseNotConfigured) -> JSONResponse:
        # Registered once here instead of try/except in every route that
        # touches storage — every storage-backed endpoint gets the same
        # clear, actionable message instead of a generic 500.
        return JSONResponse(status_code=500, content={"error": str(exc)})

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        first = exc.errors()[0] if exc.errors() else {}
        message = first.get("msg", "Invalid request")
        return JSONResponse(status_code=400, content={"error": message})

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled error on %s", request.url.path, exc_info=exc)
        return JSONResponse(status_code=500, content={"error": "Internal server error"})
