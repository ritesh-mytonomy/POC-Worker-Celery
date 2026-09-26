"""Anugrah's Upload API, /api/uploads/* (upload-ingest-merge design.md §5.1), on the Ingest internals.

Paths, request bodies and response fields are exactly those of the Upload POC's upload_routes.py, recorded in
tests/fixtures/upload_api/; the only additions are the optional `batchId` in initiate and `batchId` in its response.
Errors keep FastAPI's {"detail": "…"} body (D14) — routes/errors.py chooses the body by path.
"""
import uuid
from collections.abc import Callable
from datetime import datetime
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


# ── parts/presign and parts ─────────────────────────────────────────────────

class PresignRequest(BaseModel):
    """Anugrah's body: which parts of which upload."""

    key: str
    uploadId: str
    partNumbers: list[int]


class PresignResponse(BaseModel):
    """One presigned PUT URL per part number."""

    urls: dict[int, str]


@router.post("/parts/presign", response_model=PresignResponse)
def presign(body: PresignRequest, db: Session = Depends(get_db_session)) -> PresignResponse:
    """Presign part uploads for the browser; 404 for an unknown key + uploadId pair, 409 past uploading (U2)."""
    return PresignResponse(urls=_answer(lambda: uploads.presign(db, key=body.key, upload_id=body.uploadId,
                                                                part_numbers=body.partNumbers)))


class UploadedPart(BaseModel):
    """A part already in S3."""

    partNumber: int
    etag: str
    size: int


class ListPartsResponse(BaseModel):
    """The parts already uploaded, in order."""

    parts: list[UploadedPart]


@router.get("/{upload_id}/parts", response_model=ListPartsResponse)
def list_parts(upload_id: str, key: str, db: Session = Depends(get_db_session)) -> ListPartsResponse:
    """Parts already in S3, for resuming after a refresh (U2.1)."""
    return ListPartsResponse(parts=[UploadedPart(**p) for p in
                                    _answer(lambda: uploads.parts(db, key=key, upload_id=upload_id))])


# ── abort and the upload list ───────────────────────────────────────────────

class AbortRequest(BaseModel):
    """Anugrah's body: which multipart upload."""

    key: str
    uploadId: str


@router.post("/abort")
def abort(body: AbortRequest, db: Session = Depends(get_db_session)) -> dict[str, bool]:
    """Cancel a staged or uploading file; for a later status, change nothing (D15). Always {"ok": true}."""
    _answer(lambda: uploads.abort(db, key=body.key, upload_id=body.uploadId))
    return {"ok": True}


class UploadRecordResponse(BaseModel):
    """One upload, in Anugrah's list shape."""

    id: str
    filename: str
    size_bytes: int
    content_type: str | None
    s3_key: str
    s3_location: str | None
    status: str
    parent_id: str | None = None
    source_path: str | None = None
    created_at: datetime


@router.get("", response_model=list[UploadRecordResponse])
def list_uploads(db: Session = Depends(get_db_session)) -> list[UploadRecordResponse]:
    """The organization's uploads past `uploading`, newest first (U4.2)."""
    return [UploadRecordResponse(**r) for r in _answer(lambda: uploads.list_uploads(db))]
