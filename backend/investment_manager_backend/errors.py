"""Uniform, Italian-language API error responses."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException


_STATUS_CODES = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    422: "validation_error",
    503: "service_unavailable",
}

_DEFAULT_MESSAGES = {
    400: "La richiesta non è valida.",
    401: "Autenticazione richiesta.",
    403: "Non hai i permessi per questa operazione.",
    404: "Risorsa non trovata.",
    405: "Metodo non consentito.",
    409: "La richiesta è in conflitto con lo stato attuale.",
    422: "I dati forniti non sono validi.",
    503: "Il servizio non è temporaneamente disponibile.",
}

_FRAMEWORK_MESSAGES = {
    "Bad Request",
    "Unauthorized",
    "Forbidden",
    "Not Found",
    "Method Not Allowed",
    "Conflict",
    "Unprocessable Entity",
    "Service Unavailable",
}


def _error_content(
    *, detail: Any, code: str, message: str, field: str | None = None
) -> dict[str, Any]:
    error = {"code": code, "message": message}
    if field is not None:
        error["field"] = field
    return {"detail": detail, "error": error}


def _http_detail(status_code: int, detail: Any) -> tuple[str, str | None]:
    if isinstance(detail, str) and detail and detail not in _FRAMEWORK_MESSAGES:
        return detail, None
    if isinstance(detail, dict) and detail:
        field, message = next(iter(detail.items()))
        if isinstance(message, str):
            return message, str(field)
    return _DEFAULT_MESSAGES.get(status_code, "Si è verificato un errore."), None


def register_error_handlers(app: FastAPI) -> None:
    """Install the error envelope required by the Life backend standard."""

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        del request
        message, field = _http_detail(exc.status_code, exc.detail)
        return JSONResponse(
            status_code=exc.status_code,
            content=_error_content(
                detail=exc.detail,
                code=_STATUS_CODES.get(exc.status_code, "http_error"),
                message=message,
                field=field,
            ),
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def request_validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        del request
        detail = jsonable_encoder(exc.errors())
        first_error = detail[0] if detail else {}
        location = first_error.get("loc", ())
        field = str(location[-1]) if location else None
        return JSONResponse(
            status_code=422,
            content=_error_content(
                detail=detail,
                code="validation_error",
                message="Il valore fornito non è valido.",
                field=field,
            ),
        )
