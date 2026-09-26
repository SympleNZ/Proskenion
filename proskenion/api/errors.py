"""The error envelope and the closed error-code vocabulary (spec §16.1).

Every non-2xx response carries the same shape::

    {"error": {"code", "message", "detail", "request_id"}}

``message`` is safe to display. ``detail`` is machine-readable and varies per
code. ``request_id`` appears in the response and in the log line for the
request. The vocabulary is closed: :class:`ApiError` only accepts an
:class:`ErrorCode`, so a code outside the table cannot be constructed.
WebSocket nacks draw from the same vocabulary (§16.8, B35).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from enum import StrEnum
from typing import Any, Final

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from proskenion.api.request_id import REQUEST_ID_HEADER, request_id_from_scope
from proskenion.logging import bind_request_id, reset_request_id

log = logging.getLogger(__name__)


class ErrorCode(StrEnum):
    """The closed §16.1 vocabulary, grouped by what the client should do."""

    UNAUTHENTICATED = "unauthenticated"  # re-authentication overlay
    PERMISSION_DENIED = "permission_denied"  # show and stop; no retry
    NOT_FOUND = "not_found"  # show and stop
    CONFLICT = "conflict"  # reload and offer the diff
    IN_USE = "in_use"  # show the reference list
    VALIDATION_FAILED = "validation_failed"  # field-level errors from detail
    VALUE_OUT_OF_RANGE = "value_out_of_range"  # clamp to detail.clamped
    DEVICE_UNAVAILABLE = "device_unavailable"  # show and offer retry
    RATE_LIMITED = "rate_limited"  # countdown from detail.retry_after
    INTERNAL_ERROR = "internal_error"  # show request_id; offer to copy it


HTTP_STATUS: Final[Mapping[ErrorCode, int]] = {
    ErrorCode.UNAUTHENTICATED: 401,
    ErrorCode.PERMISSION_DENIED: 403,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.CONFLICT: 409,
    ErrorCode.IN_USE: 409,
    ErrorCode.VALIDATION_FAILED: 422,
    ErrorCode.VALUE_OUT_OF_RANGE: 422,
    ErrorCode.DEVICE_UNAVAILABLE: 503,
    ErrorCode.RATE_LIMITED: 429,
    ErrorCode.INTERNAL_ERROR: 500,
}

DEFAULT_MESSAGE: Final[Mapping[ErrorCode, str]] = {
    ErrorCode.UNAUTHENTICATED: "Authentication required",
    ErrorCode.PERMISSION_DENIED: "Not permitted",
    ErrorCode.NOT_FOUND: "Not found",
    ErrorCode.CONFLICT: "The resource has changed since it was loaded",
    ErrorCode.IN_USE: "The resource is referenced elsewhere and cannot be removed",
    ErrorCode.VALIDATION_FAILED: "The request failed validation",
    ErrorCode.VALUE_OUT_OF_RANGE: "A value is outside its permitted range",
    ErrorCode.DEVICE_UNAVAILABLE: "The device is not available",
    ErrorCode.RATE_LIMITED: "Too many requests",
    ErrorCode.INTERNAL_ERROR: "An internal error occurred. Quote the request id when reporting it.",
}

# HTTPExceptions raised by Starlette, FastAPI or our own code, by status.
# Anything not listed has no code in the vocabulary and becomes internal_error.
_STATUS_TO_CODE: Final[Mapping[int, ErrorCode]] = {
    401: ErrorCode.UNAUTHENTICATED,
    403: ErrorCode.PERMISSION_DENIED,
    404: ErrorCode.NOT_FOUND,
    409: ErrorCode.CONFLICT,
    422: ErrorCode.VALIDATION_FAILED,
    429: ErrorCode.RATE_LIMITED,
    503: ErrorCode.DEVICE_UNAVAILABLE,
}


class ApiError(Exception):
    """An error the API reports to the client with a code from the vocabulary.

    ``message`` is shown to the user, so write it for them. ``detail`` is for
    the client code and its shape is fixed per code (§16.1). ``headers`` are
    added to the response — e.g. ``Retry-After`` alongside ``rate_limited``.
    """

    def __init__(
        self,
        code: ErrorCode,
        message: str | None = None,
        detail: Mapping[str, Any] | None = None,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        if not isinstance(code, ErrorCode):
            raise TypeError(f"ApiError code must be an ErrorCode, not {type(code).__name__}")
        self.code = code
        self.message = message if message is not None else DEFAULT_MESSAGE[code]
        self.detail: dict[str, Any] | None = dict(detail) if detail is not None else None
        self.headers: dict[str, str] | None = dict(headers) if headers is not None else None
        super().__init__(f"{code.value}: {self.message}")

    @property
    def status(self) -> int:
        return HTTP_STATUS[self.code]


def error_response(
    request: Request,
    code: ErrorCode,
    message: str,
    detail: Mapping[str, Any] | None = None,
    *,
    status: int | None = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    """Render the §16.1 envelope. ``status`` defaults to the code's HTTP status."""
    request_id = request_id_from_scope(request.scope)
    body = {
        "error": {
            "code": code.value,
            "message": message,
            "detail": dict(detail) if detail is not None else None,
            "request_id": request_id,
        }
    }
    response = JSONResponse(
        body,
        status_code=status if status is not None else HTTP_STATUS[code],
        headers=dict(headers) if headers else None,
    )
    if request_id is not None:
        # Starlette's outermost 500 handler bypasses the request-id middleware,
        # so the envelope sets the header itself.
        response.headers[REQUEST_ID_HEADER] = request_id
    return response


async def _handle_api_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, ApiError)
    log.info(
        "%s %s -> %s %s",
        request.method,
        request.url.path,
        exc.status,
        exc.code.value,
        extra={"code": exc.code.value, "status": exc.status, "path": request.url.path},
    )
    return error_response(request, exc.code, exc.message, exc.detail, headers=exc.headers)


async def _handle_validation_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    fields = [
        {
            "field": ".".join(str(part) for part in error["loc"]),
            "message": error["msg"],
            "type": error["type"],
        }
        for error in exc.errors()
    ]
    log.info(
        "%s %s -> 422 validation_failed",
        request.method,
        request.url.path,
        extra={"code": ErrorCode.VALIDATION_FAILED.value, "status": 422, "fields": fields},
    )
    return error_response(
        request,
        ErrorCode.VALIDATION_FAILED,
        DEFAULT_MESSAGE[ErrorCode.VALIDATION_FAILED],
        {"fields": fields},
    )


async def _handle_http_exception(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, HTTPException)
    code = _STATUS_TO_CODE.get(exc.status_code, ErrorCode.INTERNAL_ERROR)
    message = exc.detail if isinstance(exc.detail, str) and exc.detail else DEFAULT_MESSAGE[code]
    log.log(
        logging.ERROR if exc.status_code >= 500 else logging.INFO,
        "%s %s -> %s %s",
        request.method,
        request.url.path,
        exc.status_code,
        code.value,
        extra={"code": code.value, "status": exc.status_code, "path": request.url.path},
    )
    # The original status is preserved (a 405 stays a 405) so the transport
    # semantics survive even where the vocabulary has no matching code.
    return error_response(request, code, message, status=exc.status_code, headers=exc.headers)


async def _handle_unhandled(request: Request, exc: Exception) -> JSONResponse:
    # This runs in Starlette's outermost middleware, after the request-id
    # middleware has reset the logging context; rebind so the log line carries
    # the same id the client receives.
    request_id = request_id_from_scope(request.scope)
    token = bind_request_id(request_id)
    try:
        log.error(
            "Unhandled %s while handling %s %s",
            type(exc).__name__,
            request.method,
            request.url.path,
            exc_info=exc,
            extra={
                "code": ErrorCode.INTERNAL_ERROR.value,
                "status": 500,
                "path": request.url.path,
            },
        )
    finally:
        reset_request_id(token)
    # Never echo the exception text: it may contain paths, addresses or values.
    return error_response(
        request, ErrorCode.INTERNAL_ERROR, DEFAULT_MESSAGE[ErrorCode.INTERNAL_ERROR]
    )


def register_error_handlers(app: FastAPI) -> None:
    """Route every failure path through the envelope."""
    app.add_exception_handler(ApiError, _handle_api_error)
    app.add_exception_handler(RequestValidationError, _handle_validation_error)
    app.add_exception_handler(HTTPException, _handle_http_exception)
    app.add_exception_handler(Exception, _handle_unhandled)
