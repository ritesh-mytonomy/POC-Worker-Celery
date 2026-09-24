"""Public upload routes (design.md §6.1)."""
import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db import get_db_session
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
