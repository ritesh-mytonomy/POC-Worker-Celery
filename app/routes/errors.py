"""One JSON error envelope for the API: {"error": {"code", "message"}}. 4xx never retried, 5xx retried."""
from fastapi import FastAPI, Request
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


async def _api_error(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, ApiError)
    return error_response(exc.status, exc.code, exc.message)


async def _http_error(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)
    detail = exc.detail if isinstance(exc.detail, str) else ""
    code = detail if detail.replace("_", "").isalpha() and detail.islower() else _HTTP_CODES.get(
        exc.status_code, "http_error")
    return error_response(exc.status_code, code, _MESSAGES.get(code, detail or code))


async def _validation_error(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    message = "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors())
    return error_response(400, "validation_error", message)


async def _claim_superseded(_: Request, exc: Exception) -> JSONResponse:
    return error_response(409, "claim_superseded", str(exc))


async def _identity_mismatch(_: Request, exc: Exception) -> JSONResponse:
    return error_response(409, "candidate_identity_mismatch", str(exc))


async def _invalid_input(_: Request, exc: Exception) -> JSONResponse:
    return error_response(400, "validation_error", str(exc))


async def _unexpected(request: Request, exc: Exception) -> JSONResponse:
    log.exception("unhandled_error", path=request.url.path, error=repr(exc))
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
