"""Uploads: confirm and hand to the background (R2), and Anugrah's Upload API behind /api/uploads/*
(upload-ingest-merge U1–U4, design.md §5.1)."""
import math
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, TypeVar

from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy.orm import Session

from app import storage, tasks_client
from app.config import get_settings
from app.logging import get_logger
from app.repositories import documents, files

log = get_logger(__name__)
Enqueue = Callable[[str, str], None]


@dataclass(frozen=True)
class Confirmed:
    """What confirm returns to the caller."""

    status: str
    enqueued: bool


def confirm(db: Session, file_id: uuid.UUID, enqueue: Enqueue | None = None) -> Confirmed | None:
    """staged|uploading → uploaded, commit, then enqueue once; later statuses come back unchanged. None if unknown."""
    result = files.confirm_upload(db, file_id)   # commits before returning
    if result is None:
        return None
    if not result.transitioned:
        return Confirmed(result.status, enqueued=False)
    try:
        (enqueue or tasks_client.enqueue_process_upload)(str(file_id), str(result.organization_id))
    except Exception as exc:
        # R2.6: the file stays `uploaded`; the reconcile sweeper re-enqueues it.
        log.warning("enqueue_failed", file_id=str(file_id), error=repr(exc))
        return Confirmed("uploaded", enqueued=False)
    log.info("enqueued", file_id=str(file_id))
    return Confirmed("uploaded", enqueued=True)


# ── Anugrah's Upload API ────────────────────────────────────────────────────
# Error texts are his, word for word (U12.1); his client shows `detail` and spots duplicates by the word.

T = TypeVar("T")
MIB = 1024 * 1024
S3_NOT_FOUND = {"NoSuchUpload", "NoSuchKey", "NoSuchBucket"}


class UploadRefused(Exception):
    """An Upload API refusal: the route answers `status` with {"detail": detail}."""

    def __init__(self, status: int, detail: str) -> None:
        """Record the HTTP status and the detail text."""
        super().__init__(detail)
        self.status, self.detail = status, detail


def duplicate_message(filename: str) -> str:
    """Anugrah's duplicate text; the client marks a row Duplicate because it contains the word."""
    return f"Duplicate file — {filename} has already been uploaded."


def _s3(call: Callable[..., T], *args: Any) -> T:
    """Run one storage call, turning S3 failures into Anugrah's responses: 404/502 "S3 error (…)", 500 otherwise."""
    try:
        return call(*args)
    except ClientError as exc:
        error = exc.response.get("Error", {})
        code, message = error.get("Code", ""), error.get("Message", str(exc))
        raise UploadRefused(404 if code in S3_NOT_FOUND else 502, f"S3 error ({code}): {message}") from exc
    except BotoCoreError as exc:
        raise UploadRefused(500, f"Upload storage failed: {exc}") from exc


def _organization() -> uuid.UUID:
    """The one POC organization; auth and organizations are out of scope (requirements §1.2)."""
    organization_id = get_settings().POC_ORGANIZATION_ID
    if organization_id is None:
        raise UploadRefused(503, "POC_ORGANIZATION_ID is not set")
    return organization_id


def incoming_key(organization_id: uuid.UUID, batch_id: uuid.UUID, file_id: uuid.UUID, filename: str) -> str:
    """The server-built key (U1.6): ClinSync/incoming/{org}/{batch}/{file_id}_{name}. Opaque to the client."""
    name = PurePosixPath(filename or "upload.bin").name
    return f"ClinSync/incoming/{organization_id}/{batch_id}/{file_id}_{name}"


def is_duplicate(db: Session, filename: str, file_size: int) -> bool:
    """A library document with this name (case-insensitive) and size exists (U6.1)."""
    found = documents.find_by_name_size(db, _organization(), filename, file_size)
    db.rollback()                        # read-only; end the transaction
    return found is not None


@dataclass(frozen=True)
class Initiated:
    """What initiate returns: Anugrah's fields plus the batch."""

    file_id: uuid.UUID
    batch_id: uuid.UUID
    upload_id: str
    key: str
    part_size: int
    total_parts: int


def initiate(db: Session, *, filename: str, file_size: int, content_type: str | None,
             batch_id: uuid.UUID | None) -> Initiated:
    """Register a file and start its multipart upload (U1): 413 too big, 400 type, 409 duplicate or closed batch."""
    s = get_settings()
    if file_size > s.MAX_UPLOAD_BYTES:
        raise UploadRefused(413, f"File exceeds the {s.MAX_UPLOAD_BYTES // MIB} MB limit for cloud uploads.")
    if files.file_ext_of(filename) not in s.ALLOWED_TOP_LEVEL_EXT:
        raise UploadRefused(400, f"Unsupported file type: {filename}")
    organization_id = _organization()
    if is_duplicate(db, filename, file_size):
        raise UploadRefused(409, duplicate_message(filename))
    batch_id = batch_id or uuid.uuid4()
    batch_status = files.ensure_batch(db, batch_id, organization_id, s.POC_USER_ID)
    if batch_status in ("committed", "abandoned"):
        raise UploadRefused(409, f"Batch {batch_id} is already {batch_status}.")

    file_id = uuid.uuid4()
    key = incoming_key(organization_id, batch_id, file_id, filename)
    upload_id = _s3(storage.create_multipart, key, content_type)
    try:
        files.create_upload(db, file_id=file_id, batch_id=batch_id, organization_id=organization_id,
                            file_name=filename, size_bytes=file_size, content_type=content_type, s3_key=key,
                            upload_id=upload_id)
        db.commit()
    except Exception:
        db.rollback()
        storage.abort_multipart(key, upload_id)      # Anugrah's order: no multipart upload without its row
        raise
    log.info("upload_initiated", file_id=str(file_id), batch_id=str(batch_id), size_bytes=file_size)
    return Initiated(file_id=file_id, batch_id=batch_id, upload_id=upload_id, key=key,
                     part_size=s.UPLOAD_PART_SIZE_BYTES,
                     total_parts=max(1, math.ceil(file_size / s.UPLOAD_PART_SIZE_BYTES)))


def _awaiting(db: Session, upload_id: str, key: str, *, lock: bool = False) -> files.UploadRow:
    """The file for this key + uploadId pair, still staged or uploading: else 404 or 409 (U2.2)."""
    row = files.find_by_upload(db, upload_id, key, lock=lock)
    if row is None:
        db.rollback()
        raise UploadRefused(404, "Upload not found.")
    if row.status not in ("staged", "uploading"):
        db.rollback()
        raise UploadRefused(409, "Upload is not awaiting completion.")
    return row


def presign(db: Session, *, key: str, upload_id: str, part_numbers: list[int]) -> dict[int, str]:
    """Presigned PUT URLs on the public host, one per part (U2); the first presign moves staged → uploading."""
    if not part_numbers:
        raise UploadRefused(400, "partNumbers must not be empty.")
    row = _awaiting(db, upload_id, key)
    files.mark_uploading(db, row.file_id)
    return {n: _s3(storage.presign_part, row.s3_key, row.upload_id, n) for n in part_numbers}


def parts(db: Session, *, key: str, upload_id: str) -> list[dict[str, Any]]:
    """Parts already in S3, for resuming (U2.1); same pair check as presign."""
    row = _awaiting(db, upload_id, key)
    db.rollback()                        # read-only; end the transaction before the S3 call
    try:
        return _s3(storage.list_parts, row.s3_key, row.upload_id)
    except storage.NoSuchUpload as exc:
        raise UploadRefused(404, f"S3 error ({exc.code or 'NoSuchUpload'}): {exc}") from exc


def abort(db: Session, *, key: str, upload_id: str) -> None:
    """Cancel a staged or uploading file: abort its multipart upload, end it `error` "Upload cancelled" (U4.1).

    Any later status is left exactly as it is (D15): the client also sends abort when a finished row is removed.
    The row lock makes a racing complete either finish first (abort is then a no-op) or see the file cancelled.
    """
    row = files.find_by_upload(db, upload_id, key, lock=True)
    if row is None:
        db.rollback()
        raise UploadRefused(404, "Upload not found.")
    if row.status not in ("staged", "uploading"):
        db.rollback()
        log.info("abort_ignored", file_id=str(row.file_id), status=row.status)
        return
    try:
        _s3(storage.abort_multipart, row.s3_key, row.upload_id)
    except UploadRefused:
        db.rollback()                    # S3 still has the upload: leave the file as it was
        raise
    files.cancel_upload(db, row.file_id)
    db.commit()
    log.info("upload_cancelled", file_id=str(row.file_id), batch_id=str(row.batch_id))


def list_uploads(db: Session) -> list[dict[str, Any]]:
    """Uploads past `uploading`, newest first, in Anugrah's GET /api/uploads shape (U4.2).

    s3_location, parent_id and source_path have no LLD column and merged uploads have no parent: they are null.
    """
    rows = files.list_uploads(db, _organization())
    db.rollback()
    return [{"id": str(r["file_id"]), "filename": r["file_name"], "size_bytes": r["size_bytes"],
             "content_type": r["content_type"], "s3_key": r["s3_key"], "s3_location": None, "status": r["status"],
             "parent_id": None, "source_path": None, "created_at": r["created_at"]} for r in rows]


@dataclass(frozen=True)
class Completed:
    """What complete returns: Anugrah's fields plus the batch."""

    file_id: uuid.UUID
    batch_id: uuid.UUID
    key: str
    location: str
    status: str


def complete(db: Session, *, key: str, upload_id: str, parts: list[dict[str, Any]]) -> Completed:
    """Finish the S3 upload, then hand over exactly as the Ingest confirm does (U3, design.md §5.3).

    No download, no hashing, no validation — the Worker does those (U3.3). Called again for a file already past
    uploading, it returns the current status and enqueues nothing (U3.5). If S3 refuses the parts, or the upload
    has expired, the file's status does not change, so the client can retry (U3.6).
    """
    if not parts:
        raise UploadRefused(400, "parts must not be empty.")
    row = files.find_by_upload(db, upload_id, key)
    db.rollback()                        # read-only so far; confirm below runs its own transaction
    if row is None:
        raise UploadRefused(404, "Upload not found.")
    location = f"s3://{get_settings().S3_BUCKET}/{row.s3_key}"   # stable; never the internal hostname
    if row.status not in ("staged", "uploading"):
        return Completed(row.file_id, row.batch_id, row.s3_key, location, row.status)
    try:
        _s3(storage.complete_multipart, row.s3_key, row.upload_id, parts)
    except storage.NoSuchUpload as exc:
        # An earlier complete may have assembled the object and died before confirm: carry on if it is there.
        if not _s3(storage.exists, row.s3_key):
            raise UploadRefused(400, "Upload session expired — start the upload again") from exc
        log.info("complete_retry_after_s3_completion", file_id=str(row.file_id))
    except storage.InvalidParts as exc:
        raise UploadRefused(400, f"S3 error ({exc.code}): {exc}") from exc
    result = confirm(db, row.file_id)    # the Ingest confirm: staged|uploading → uploaded, commit, THEN enqueue
    status = result.status if result else row.status
    log.info("upload_completed", file_id=str(row.file_id), batch_id=str(row.batch_id), status=status,
             enqueued=bool(result and result.enqueued))
    return Completed(row.file_id, row.batch_id, row.s3_key, location, status)
