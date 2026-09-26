"""Error bodies, chosen by route (upload-ingest-merge D14, U12).

/api/v1/* and /internal/* use one JSON envelope: {"error": {"code", "message"}}; 4xx never retried, 5xx retried.
Anugrah's Upload API, /api/uploads/*, keeps FastAPI's own bodies exactly as his client reads them: {"detail": "…"},
and 422 {"detail": [...]} for an invalid request body.
"""
from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.errors import CandidateIdentityMismatch, ClaimSuperseded, InvalidInput
from app.logging import get_logger

log = get_logger(__name__)

_HTTP_CODES = {404: "not_found", 405: "method_not_allowed"}
_MESSAGES = {"invalid_internal_key": "Missing or invalid X-Internal-Key"}


class ApiError(Exception):
    """An error with a fixed HTTP status and machine-readable code."""

    def __init__(self, status: int, code: str, message: str) -> None:
        """Record status, code and message."""
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def error_response(status: int, code: str, message: str) -> JSONResponse:
    """Build the error envelope."""
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message}})


def is_upload_api(request: Request) -> bool:
    """True for Anugrah's /api/uploads/* routes, whose errors keep the {"detail": …} body."""
    path = request.url.path
    return path == "/api/uploads" or path.startswith("/api/uploads/")


def detail_response(status: int, detail: Any, headers: dict[str, str] | None = None) -> JSONResponse:
    """Anugrah's error body: FastAPI's {"detail": …}."""
    return JSONResponse(status_code=status, content={"detail": detail}, headers=headers)


async def _api_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, ApiError)
    if is_upload_api(request):
        return detail_response(exc.status, exc.message)
    return error_response(exc.status, exc.code, exc.message)


async def _http_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)
    if is_upload_api(request):
        return detail_response(exc.status_code, exc.detail, getattr(exc, "headers", None))
    detail = exc.detail if isinstance(exc.detail, str) else ""
    code = detail if detail.replace("_", "").isalpha() and detail.islower() else _HTTP_CODES.get(
        exc.status_code, "http_error")
    return error_response(exc.status_code, code, _MESSAGES.get(code, detail or code))


async def _validation_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    if is_upload_api(request):                     # FastAPI's own 422, as Anugrah's API answered
        return detail_response(422, jsonable_encoder(exc.errors()))
    message = "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors())
    return error_response(400, "validation_error", message)


async def _claim_superseded(_: Request, exc: Exception) -> JSONResponse:
    return error_response(409, "claim_superseded", str(exc))


async def _identity_mismatch(_: Request, exc: Exception) -> JSONResponse:
    return error_response(409, "candidate_identity_mismatch", str(exc))


async def _invalid_input(request: Request, exc: Exception) -> JSONResponse:
    if is_upload_api(request):
        return detail_response(400, str(exc))
    return error_response(400, "validation_error", str(exc))


async def _unexpected(request: Request, exc: Exception) -> JSONResponse:
    log.exception("unhandled_error", path=request.url.path, error=repr(exc))
    if is_upload_api(request):
        return detail_response(500, "Internal server error")
    return error_response(500, "internal_error", "Internal server error")


def install_error_handlers(app: FastAPI) -> None:
    """Map every error the API raises to the envelope and its status (most specific class wins)."""
    app.add_exception_handler(ApiError, _api_error)
    app.add_exception_handler(StarletteHTTPException, _http_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(ClaimSuperseded, _claim_superseded)
    app.add_exception_handler(CandidateIdentityMismatch, _identity_mismatch)
    app.add_exception_handler(InvalidInput, _invalid_input)
    app.add_exception_handler(Exception, _unexpected)
