"""Anugrah's Upload API, /api/uploads/* (upload-ingest-merge design.md §5.1), on the Ingest internals.

Paths, request bodies and response fields are exactly those of the Upload POC's upload_routes.py, recorded in
tests/fixtures/upload_api/; the only additions are the optional `batchId` in initiate and `batchId` in its response.
Errors keep FastAPI's {"detail": "…"} body (D14) — app/error_handlers.py chooses the body by path.
"""
from collections.abc import Callable
from typing import TypeVar

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import get_db_session
from app.services import uploads
from app.schemas import (
    AbortRequest,
    CompleteRequest,
    CompleteResponse,
    DuplicateCheckRequest,
    DuplicateCheckResponse,
    InitiateRequest,
    InitiateResponse,
    ListPartsResponse,
    PresignRequest,
    PresignResponse,
    UploadRecordResponse,
    UploadedPart,
)

router = APIRouter(prefix="/api/uploads", tags=["uploads"])
T = TypeVar("T")


def _answer(call: Callable[[], T]) -> T:
    """Run a service call; an UploadRefused becomes its HTTP status with {"detail": …}."""
    try:
        return call()
    except uploads.UploadRefused as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from exc


# ── check-duplicate ─────────────────────────────────────────────────────────

@router.post("/check-duplicate", response_model=DuplicateCheckResponse)
def check_duplicate(body: DuplicateCheckRequest, db: Session = Depends(get_db_session)) -> DuplicateCheckResponse:
    """Match against library documents by name (case-insensitive) and size (U6.1)."""
    if _answer(lambda: uploads.is_duplicate(db, body.filename, body.fileSize)):
        return DuplicateCheckResponse(duplicate=True, message=uploads.duplicate_message(body.filename))
    return DuplicateCheckResponse(duplicate=False)


# ── initiate ────────────────────────────────────────────────────────────────

@router.post("/initiate", response_model=InitiateResponse)
def initiate(body: InitiateRequest, db: Session = Depends(get_db_session)) -> InitiateResponse:
    """Register the file in its batch and start the multipart upload (U1)."""
    started = _answer(lambda: uploads.initiate(db, filename=body.filename, file_size=body.fileSize,
                                               content_type=body.contentType, batch_id=body.batchId))
    return InitiateResponse(id=str(started.file_id), fileId=str(started.file_id), uploadId=started.upload_id,
                            key=started.key, partSize=started.part_size, totalParts=started.total_parts,
                            batchId=str(started.batch_id))


# ── parts/presign and parts ─────────────────────────────────────────────────

@router.post("/parts/presign", response_model=PresignResponse)
def presign(body: PresignRequest, db: Session = Depends(get_db_session)) -> PresignResponse:
    """Presign part uploads for the browser; 404 for an unknown key + uploadId pair, 409 past uploading (U2)."""
    return PresignResponse(urls=_answer(lambda: uploads.presign(db, key=body.key, upload_id=body.uploadId,
                                                                part_numbers=body.partNumbers)))


@router.get("/{upload_id}/parts", response_model=ListPartsResponse)
def list_parts(upload_id: str, key: str, db: Session = Depends(get_db_session)) -> ListPartsResponse:
    """Parts already in S3, for resuming after a refresh (U2.1)."""
    return ListPartsResponse(parts=[UploadedPart(**p) for p in
                                    _answer(lambda: uploads.parts(db, key=key, upload_id=upload_id))])


# ── abort and the upload list ───────────────────────────────────────────────

@router.post("/abort")
def abort(body: AbortRequest, db: Session = Depends(get_db_session)) -> dict[str, bool]:
    """Cancel a staged or uploading file; for a later status, change nothing (D15). Always {"ok": true}."""
    _answer(lambda: uploads.abort(db, key=body.key, upload_id=body.uploadId))
    return {"ok": True}


@router.get("", response_model=list[UploadRecordResponse])
def list_uploads(db: Session = Depends(get_db_session)) -> list[UploadRecordResponse]:
    """The organization's uploads past `uploading`, newest first (U4.2)."""
    return [UploadRecordResponse(**r) for r in _answer(lambda: uploads.list_uploads(db))]


# ── complete: the join point ────────────────────────────────────────────────

@router.post("/complete", response_model=CompleteResponse)
def complete(body: CompleteRequest, db: Session = Depends(get_db_session)) -> CompleteResponse:
    """Complete the S3 upload and hand the file to the Ingest pipeline, as confirm does (U3)."""
    done = _answer(lambda: uploads.complete(db, key=body.key, upload_id=body.uploadId,
                                            parts=[p.model_dump() for p in body.parts]))
    return CompleteResponse(id=str(done.file_id), location=done.location, key=done.key, status=done.status,
                            batchId=str(done.batch_id))
