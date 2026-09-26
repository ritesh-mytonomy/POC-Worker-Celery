"""The Library (upload-ingest-merge U9): list the organization's documents, and download one."""
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import storage
from app.config import get_settings
from app.db import get_db_session
from app.repositories import documents
from app.routes.errors import ApiError

router = APIRouter(prefix="/api/v1/library")


class DocumentOut(BaseModel):
    """One library document."""

    document_id: uuid.UUID
    title: str
    file_name: str
    file_ext: str
    size_bytes: int
    created_at: datetime


class LibraryOut(BaseModel):
    """The library, newest first."""

    documents: list[DocumentOut]
    total: int


class DownloadOut(BaseModel):
    """A short-lived presigned download URL, signed for the host the browser reaches."""

    url: str
    expires_at: datetime


def _organization() -> uuid.UUID:
    """The one POC organization (auth is out of scope)."""
    organization_id = get_settings().POC_ORGANIZATION_ID
    if organization_id is None:
        raise ApiError(503, "poc_not_configured", "POC_ORGANIZATION_ID is not set")
    return organization_id


@router.get("/documents", response_model=LibraryOut)
def list_documents(db: Session = Depends(get_db_session)) -> LibraryOut:
    """Library documents, newest first, with a stable order for documents added together (U9.1)."""
    rows = documents.list_library(db, _organization())
    return LibraryOut(documents=[DocumentOut(**r) for r in rows], total=len(rows))


@router.get("/documents/{document_id}/download", response_model=DownloadOut)
def download(document_id: uuid.UUID, db: Session = Depends(get_db_session)) -> DownloadOut:
    """A presigned GET on the public host, saving under the file name; 404 for an unknown document (U9.2)."""
    doc = documents.get(db, _organization(), document_id)
    if doc is None:
        raise ApiError(404, "not_found", f"document {document_id} does not exist")
    seconds = get_settings().DOWNLOAD_URL_EXPIRES_SECONDS
    return DownloadOut(url=storage.presign_get(doc["s3_key"], doc["file_name"], seconds),
                       expires_at=datetime.now(timezone.utc) + timedelta(seconds=seconds))
