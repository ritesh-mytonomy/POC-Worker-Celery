"""FastAPI entry point: routers, error envelope, startup validation."""
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.logging import configure_logging, get_logger
from app.routes import internal, upload_api, uploads
from app.routes.errors import install_error_handlers

configure_logging()
log = get_logger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Refuse to start on an invalid configuration (R9.5)."""
    try:
        get_settings().validate()
    except ValueError as exc:
        log.error("invalid_configuration", error=str(exc))
        raise
    log.info("api_started")
    yield


app = FastAPI(title="ClinSync Ingest POC", lifespan=lifespan)
install_error_handlers(app)
# The browser client (http://localhost:5173) calls /api/uploads/* and /api/v1/* directly; it reads ETag from S3,
# not from us, but exposing it matches Anugrah's API (upload-ingest-merge design.md §8).
app.add_middleware(CORSMiddleware, allow_origins=get_settings().CORS_ORIGINS,
                   allow_methods=["GET", "POST", "PUT", "HEAD", "OPTIONS"], allow_headers=["*"],
                   expose_headers=["ETag"])
app.include_router(internal.router)
app.include_router(uploads.router)
app.include_router(upload_api.router)


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness probe."""
    return {"status": "ok"}
