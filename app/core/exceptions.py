from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException


logger = logging.getLogger(__name__)


class ResourceExhaustedError(RuntimeError):
    pass


def _request_id_from(request: Request) -> str:
    request_id: str | None = getattr(request.state, "request_id", None)
    return request_id or "unknown"


def _error_body(
    *,
    request: Request,
    error_code: str,
    message: str,
    detail: Any = None,
) -> dict[str, Any]:
    return {
        "error_code": error_code,
        "message": message,
        "request_id": _request_id_from(request),
        "detail": detail,
    }


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(
        request: Request,
        exc: StarletteHTTPException,
    ) -> JSONResponse:
        if exc.status_code == status.HTTP_404_NOT_FOUND:
            error_code: str = "NOT_FOUND"
            message: str = "Resource not found."
        else:
            error_code = "HTTP_ERROR"
            message = "HTTP request failed."

        return JSONResponse(
            status_code=exc.status_code,
            content=_error_body(
                request=request,
                error_code=error_code,
                message=message,
                detail=exc.detail,
            ),
        )

    @app.exception_handler(RequestValidationError)
    async def request_validation_exception_handler(
        request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content=_error_body(
                request=request,
                error_code="VALIDATION_ERROR",
                message="Request validation failed.",
                detail=exc.errors(),
            ),
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(
        request: Request,
        exc: Exception,
    ) -> JSONResponse:
        logger.error(
            "Unhandled Haypile request request_id=%s path=%s error_type=%s",
            _request_id_from(request),
            request.url.path,
            type(exc).__name__,
        )
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=_error_body(
                request=request,
                error_code="INTERNAL_SERVER_ERROR",
                message="Internal server error.",
                detail=None,
            ),
        )
