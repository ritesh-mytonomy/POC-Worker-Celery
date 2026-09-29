"""Public upload routes (design.md §6.1)."""
import uuid

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from shared.config import get_settings
from app.db import get_db_session
from app.repositories import candidates, files
from app.error_handlers import ApiError
from app.services import uploads
from app.schemas import (
    BatchOut,
    CommitOut,
    ConfirmOut,
    DuplicateOfOut,
    FileStatusOut,
    SkippedOut,
    StagedDocumentOut,
    StagedOut,
    UploadConfigOut,
)

router = APIRouter(prefix="/api/v1/uploads")


@router.post("/{file_id}/confirm", response_model=ConfirmOut)
def confirm(file_id: uuid.UUID, db: Session = Depends(get_db_session)) -> ConfirmOut:
    """Confirm an upload (R2): 200 always for a known file, 404 for an unknown one."""
    result = uploads.confirm(db, file_id)
    if result is None:
        raise ApiError(404, "not_found", f"file {file_id} does not exist")
    return ConfirmOut(file_id=file_id, status=result.status, enqueued=result.enqueued)


@router.get("/config", response_model=UploadConfigOut)
def upload_config() -> UploadConfigOut:
    """The allowed file types, zip entry types, folder depth and size limit, straight from the settings."""
    s = get_settings()
    return UploadConfigOut(allowed_top_level_ext=s.ALLOWED_TOP_LEVEL_EXT, allowed_zip_entry_ext=s.ALLOWED_ZIP_ENTRY_EXT,
                           max_upload_bytes=s.MAX_UPLOAD_BYTES, max_zip_folder_depth=s.MAX_ZIP_FOLDER_DEPTH)


@router.get("/batches/{batch_id}", response_model=BatchOut)
def batch_status(batch_id: uuid.UUID, db: Session = Depends(get_db_session)) -> BatchOut:
    """Every file of the batch with its progress (R14.1); 404 for an unknown batch."""
    rows = files.list_batch_files(db, batch_id)
    if rows is None:
        raise ApiError(404, "not_found", f"batch {batch_id} does not exist")
    return BatchOut(batch_id=batch_id, files=[FileStatusOut(**r) for r in rows],
                    ready_to_add=candidates.ready_to_add_count(db, batch_id),
                    pending_review=candidates.pending_review_count(db, batch_id),
                    in_progress=files.unfinished_count(db, batch_id))


@router.get("/batches/{batch_id}/staged", response_model=StagedOut)
def batch_staged(batch_id: uuid.UUID, db: Session = Depends(get_db_session)) -> StagedOut:
    """Every candidate of the batch (R14.2); 404 for an unknown batch."""
    rows = candidates.list_for_batch(db, batch_id)
    if rows is None:
        raise ApiError(404, "not_found", f"batch {batch_id} does not exist")
    return StagedOut(batch_id=batch_id, staged=[_staged_out(r) for r in rows])


def _staged_out(row: dict[str, object]) -> StagedDocumentOut:
    """A candidate row, its duplicate marker folded into duplicate_of."""
    marker = ("duplicate_of_document_id", "duplicate_kind", "duplicate_title")
    fields = {k: v for k, v in row.items() if k not in marker}
    duplicate = (DuplicateOfOut(document_id=row["duplicate_of_document_id"], title=row["duplicate_title"],
                                kind=row["duplicate_kind"]) if row["duplicate_of_document_id"] else None)
    return StagedDocumentOut(**fields, duplicate_of=duplicate)


@router.post("/batches/{batch_id}/commit", response_model=CommitOut)
def commit_batch(batch_id: uuid.UUID, db: Session = Depends(get_db_session)) -> CommitOut:
    """Add the batch's ready candidates to the library — incremental, idempotent (U8); 404 for an unknown batch."""
    try:
        result = uploads.commit(db, batch_id)
    except uploads.BatchNotFound:
        raise ApiError(404, "not_found", f"batch {batch_id} does not exist") from None
    except (ClientError, BotoCoreError) as exc:
        raise ApiError(502, "storage_error", f"S3 failed during the commit; nothing was added: {exc}") from exc
    return CommitOut(added=len(result.added), skipped=[SkippedOut(**s) for s in result.skipped],
                     still_in_progress=result.still_in_progress, documents=result.added)
