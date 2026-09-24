"""FastAPI entry point — minimal stub for Checkpoint A; replaced by tasks 2.x."""
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import get_settings
from app.logging import configure_logging, get_logger

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


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness probe."""
    return {"status": "ok"}
