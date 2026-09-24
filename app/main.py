"""FastAPI entry point — minimal stub for Checkpoint A; replaced by tasks 2.x."""
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import get_settings


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Refuse to start on an invalid configuration (R9.5)."""
    get_settings().validate()
    yield


app = FastAPI(title="ClinSync Ingest POC", lifespan=lifespan)


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness probe."""
    return {"status": "ok"}
