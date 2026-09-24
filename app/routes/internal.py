"""Internal API for workers (design.md §6.2). Every route needs X-Internal-Key; writes after claim need X-Claim-Token."""
import uuid

from fastapi import APIRouter, Depends, Header, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db_session
from app.repositories import candidates, files
from app.routes.errors import ApiError
from app.security import require_internal_key
from app.services import sweeps

router = APIRouter(prefix="/internal", dependencies=[Depends(require_internal_key)])


class _Body(BaseModel):
    """Request bodies reject unknown fields."""

    model_config = ConfigDict(extra="forbid")


class ProgressIn(_Body):
    """Progress fields; only those present are written."""

    entries_total: int | None = Field(default=None, ge=0)
    entries_done: int | None = Field(default=None, ge=0)
    detected_type: str | None = Field(default=None, min_length=1, max_length=32)


class CandidateIn(_Body):
    """One candidate; batch_id and organization_id come from the parent file, so they are not accepted."""

    source_entry_name: str | None = Field(default=None, min_length=1, max_length=1024)
    entry_index: int | None = Field(default=None, ge=0)
    file_name: str = Field(min_length=1, max_length=512)
    file_ext: str = Field(max_length=10)
    status: str
    size_bytes: int | None = Field(default=None, ge=0)
    s3_key: str | None = Field(default=None, max_length=1024)
    reject_reason: str | None = None


class FinishIn(_Body):
    """Terminal status and optional message."""

    status: str
    status_message: str | None = None


class ReleaseIn(_Body):
    """Why the worker is handing the file back before a retry."""

    reason: str = Field(min_length=1)


class FileClaimOut(BaseModel):
    """A successful claim (design.md §6.2)."""

    file_id: uuid.UUID
    organization_id: uuid.UUID
    batch_id: uuid.UUID
    s3_key: str
    file_name: str
    file_ext: str
    is_archive: bool
    entries_done: int
    attempt_count: int
    claim_token: uuid.UUID


class StagedOut(BaseModel):
    """The upserted candidate's id."""

    staged_id: uuid.UUID


class StaleSweepOut(BaseModel):
    """Result of a stale sweep."""

    reset: int
    errored: int
    enqueue_failed: int


class ReconcileSweepOut(BaseModel):
    """Result of a reconcile sweep."""

    requeued: int
    errored: int
    enqueue_failed: int


def claim_token(x_claim_token: str | None = Header(default=None)) -> uuid.UUID:
    """Parse X-Claim-Token; 400 invalid_claim_token when missing or not a UUID (never FastAPI's 422)."""
    try:
        return uuid.UUID(x_claim_token or "")
    except ValueError:
        raise ApiError(400, "invalid_claim_token", "X-Claim-Token header is missing or not a UUID") from None


@router.post("/files/{file_id}/claim", response_model=FileClaimOut)
def claim_file(file_id: uuid.UUID, db: Session = Depends(get_db_session)) -> FileClaimOut:
    """Claim the file; 409 not_claimable if another worker owns it or it is not claimable (R3)."""
    s = get_settings()
    # First statement in this fresh session: claim() relies on now() being this transaction's start.
    result = files.claim(db, file_id, max_attempts=s.MAX_ATTEMPTS, stale_after_seconds=s.STALE_AFTER_SECONDS)
    if result is None:
        raise ApiError(409, "not_claimable", f"file {file_id} is not claimable")
    return FileClaimOut(**vars(result))


@router.post("/files/{file_id}/heartbeat", status_code=status.HTTP_204_NO_CONTENT)
def heartbeat(file_id: uuid.UUID, token: uuid.UUID = Depends(claim_token),
              db: Session = Depends(get_db_session)) -> Response:
    """Refresh the heartbeat (R10.1)."""
    files.heartbeat(db, file_id, token)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.patch("/files/{file_id}/progress", status_code=status.HTTP_204_NO_CONTENT)
def progress(file_id: uuid.UUID, body: ProgressIn, token: uuid.UUID = Depends(claim_token),
             db: Session = Depends(get_db_session)) -> Response:
    """Record progress (R7.1, R8.1)."""
    files.progress(db, file_id, token, **body.model_dump())
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put("/files/{file_id}/candidates", response_model=StagedOut)
def upsert_candidate(file_id: uuid.UUID, body: CandidateIn, token: uuid.UUID = Depends(claim_token),
                     db: Session = Depends(get_db_session)) -> StagedOut:
    """Insert or overwrite one candidate (R8.3)."""
    return StagedOut(staged_id=candidates.upsert(db, file_id, token, **body.model_dump()))


@router.post("/files/{file_id}/finish", status_code=status.HTTP_204_NO_CONTENT)
def finish(file_id: uuid.UUID, body: FinishIn, token: uuid.UUID = Depends(claim_token),
           db: Session = Depends(get_db_session)) -> Response:
    """Set the terminal status (R10.3)."""
    files.finish(db, file_id, token, body.status, body.status_message)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/files/{file_id}/release", status_code=status.HTTP_204_NO_CONTENT)
def release(file_id: uuid.UUID, body: ReleaseIn, token: uuid.UUID = Depends(claim_token),
            db: Session = Depends(get_db_session)) -> Response:
    """Hand the file back before a retry; the repository logs the reason with file_id and attempt."""
    files.release(db, file_id, token, reason=body.reason)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/sweeps/stale", response_model=StaleSweepOut)
def stale_sweep(db: Session = Depends(get_db_session)) -> StaleSweepOut:
    """Run the stale sweep (R11.4)."""
    s = get_settings()
    return StaleSweepOut(**sweeps.stale_sweep(db, stale_after_seconds=s.STALE_AFTER_SECONDS,
                                              max_attempts=s.MAX_ATTEMPTS))


@router.post("/sweeps/reconcile", response_model=ReconcileSweepOut)
def reconcile_sweep(db: Session = Depends(get_db_session)) -> ReconcileSweepOut:
    """Run the reconcile sweep (R11.5)."""
    s = get_settings()
    return ReconcileSweepOut(**sweeps.reconcile_sweep(db, reconcile_after_seconds=s.RECONCILE_AFTER_SECONDS,
                                                      max_attempts=s.MAX_ATTEMPTS))
