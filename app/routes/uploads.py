"""Public upload routes (design.md §6.1)."""
import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db import get_db_session
from app.repositories import candidates, files
from app.routes.errors import ApiError
from app.services import uploads

router = APIRouter(prefix="/api/v1/uploads")


class ConfirmOut(BaseModel):
    """Status after confirm, and whether this call enqueued the file."""

    file_id: uuid.UUID
    status: str
    enqueued: bool


@router.post("/{file_id}/confirm", response_model=ConfirmOut)
def confirm(file_id: uuid.UUID, db: Session = Depends(get_db_session)) -> ConfirmOut:
    """Confirm an upload (R2): 200 always for a known file, 404 for an unknown one."""
    result = uploads.confirm(db, file_id)
    if result is None:
        raise ApiError(404, "not_found", f"file {file_id} does not exist")
    return ConfirmOut(file_id=file_id, status=result.status, enqueued=result.enqueued)


class FileStatusOut(BaseModel):
    """One file's status (R14.1)."""

    file_id: uuid.UUID
    file_name: str
    status: str
    entries_total: int | None
    entries_done: int
    attempt_count: int
    detected_type: str | None
    status_message: str | None


class BatchOut(BaseModel):
    """A batch and its files."""

    batch_id: uuid.UUID
    files: list[FileStatusOut]


class StagedDocumentOut(BaseModel):
    """One candidate (R14.2); the staging s3_key stays internal."""

    staged_id: uuid.UUID
    source_file_id: uuid.UUID
    source_entry_name: str | None
    entry_index: int | None
    file_name: str
    status: str
    reject_reason: str | None


class StagedOut(BaseModel):
    """A batch's candidates."""

    batch_id: uuid.UUID
    staged: list[StagedDocumentOut]


@router.get("/batches/{batch_id}", response_model=BatchOut)
def batch_status(batch_id: uuid.UUID, db: Session = Depends(get_db_session)) -> BatchOut:
    """Every file of the batch with its progress (R14.1); 404 for an unknown batch."""
    rows = files.list_batch_files(db, batch_id)
    if rows is None:
        raise ApiError(404, "not_found", f"batch {batch_id} does not exist")
    return BatchOut(batch_id=batch_id, files=[FileStatusOut(**r) for r in rows])


@router.get("/batches/{batch_id}/staged", response_model=StagedOut)
def batch_staged(batch_id: uuid.UUID, db: Session = Depends(get_db_session)) -> StagedOut:
    """Every candidate of the batch (R14.2); 404 for an unknown batch."""
    rows = candidates.list_for_batch(db, batch_id)
    if rows is None:
        raise ApiError(404, "not_found", f"batch {batch_id} does not exist")
    return StagedOut(batch_id=batch_id, staged=[StagedDocumentOut(**r) for r in rows])
