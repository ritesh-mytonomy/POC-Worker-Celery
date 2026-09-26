"""Anugrah's Upload API, /api/uploads/* (upload-ingest-merge design.md §5.1), on the Ingest internals.

Paths, request bodies and response fields are exactly those of the Upload POC's upload_routes.py, recorded in
tests/fixtures/upload_api/; the only additions are the optional `batchId` in initiate and `batchId` in its response.
Errors keep FastAPI's {"detail": "…"} body (D14) — routes/errors.py chooses the body by path.
"""
import uuid
from collections.abc import Callable
from typing import TypeVar

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db import get_db_session
from app.services import uploads

router = APIRouter(prefix="/api/uploads", tags=["uploads"])
T = TypeVar("T")


def _answer(call: Callable[[], T]) -> T:
    """Run a service call; an UploadRefused becomes its HTTP status with {"detail": …}."""
    try:
        return call()
    except uploads.UploadRefused as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from exc


# ── check-duplicate ─────────────────────────────────────────────────────────

class DuplicateCheckRequest(BaseModel):
    """Anugrah's body: the file's name and size."""

    filename: str
    fileSize: int = Field(gt=0)


class DuplicateCheckResponse(BaseModel):
    """Whether a library document has this name and size."""

    duplicate: bool
    message: str | None = None


@router.post("/check-duplicate", response_model=DuplicateCheckResponse)
def check_duplicate(body: DuplicateCheckRequest, db: Session = Depends(get_db_session)) -> DuplicateCheckResponse:
    """Match against library documents by name (case-insensitive) and size (U6.1)."""
    if _answer(lambda: uploads.is_duplicate(db, body.filename, body.fileSize)):
        return DuplicateCheckResponse(duplicate=True, message=uploads.duplicate_message(body.filename))
    return DuplicateCheckResponse(duplicate=False)


# ── initiate ────────────────────────────────────────────────────────────────

class InitiateRequest(BaseModel):
    """Anugrah's body, plus the optional client-generated batchId (D4)."""

    filename: str
    fileSize: int = Field(gt=0)
    contentType: str | None = None
    batchId: uuid.UUID | None = None


class InitiateResponse(BaseModel):
    """Anugrah's fields, plus batchId."""

    id: str
    fileId: str
    uploadId: str
    key: str
    partSize: int
    totalParts: int
    batchId: str


@router.post("/initiate", response_model=InitiateResponse)
def initiate(body: InitiateRequest, db: Session = Depends(get_db_session)) -> InitiateResponse:
    """Register the file in its batch and start the multipart upload (U1)."""
    started = _answer(lambda: uploads.initiate(db, filename=body.filename, file_size=body.fileSize,
                                               content_type=body.contentType, batch_id=body.batchId))
    return InitiateResponse(id=str(started.file_id), fileId=str(started.file_id), uploadId=started.upload_id,
                            key=started.key, partSize=started.part_size, totalParts=started.total_parts,
                            batchId=str(started.batch_id))
