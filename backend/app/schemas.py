"""Request and response models for every API route (Pydantic), one section per router.

Field names and casing are the wire contract: /api/uploads/* keeps Anugrah's camelCase exactly (the Client sends
and reads them unchanged); /api/v1/* and /internal/* use snake_case.
"""
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


# ── Anugrah's multipart upload API — /api/uploads/* (routers/multipart.py) ────────────────────────────────────────


class DuplicateCheckRequest(BaseModel):
    """Anugrah's body: the file's name and size."""

    filename: str
    fileSize: int = Field(gt=0)


class DuplicateCheckResponse(BaseModel):
    """Whether a library document has this name and size."""

    duplicate: bool
    message: str | None = None


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


class PresignRequest(BaseModel):
    """Anugrah's body: which parts of which upload."""

    key: str
    uploadId: str
    partNumbers: list[int]


class PresignResponse(BaseModel):
    """One presigned PUT URL per part number."""

    urls: dict[int, str]


class UploadedPart(BaseModel):
    """A part already in S3."""

    partNumber: int
    etag: str
    size: int


class ListPartsResponse(BaseModel):
    """The parts already uploaded, in order."""

    parts: list[UploadedPart]


class AbortRequest(BaseModel):
    """Anugrah's body: which multipart upload."""

    key: str
    uploadId: str


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


class PartInput(BaseModel):
    """One uploaded part and the ETag S3 gave it."""

    partNumber: int
    etag: str


class CompleteRequest(BaseModel):
    """Anugrah's body, unchanged; id, fileId, filename, fileSize and contentType are accepted, not needed."""

    id: str | None = None
    fileId: str | None = None
    key: str
    uploadId: str
    filename: str
    fileSize: int = Field(gt=0)
    contentType: str | None = None
    parts: list[PartInput]


class CompleteResponse(BaseModel):
    """Anugrah's fields, plus batchId; status is now `uploaded` (U3.4)."""

    id: str
    location: str
    key: str
    status: str
    batchId: str


# ── Batches, review and commit — /api/v1/uploads/* (routers/uploads.py) ───────────────────────────────────────────


class ConfirmOut(BaseModel):
    """Status after confirm, and whether this call enqueued the file."""

    file_id: uuid.UUID
    status: str
    enqueued: bool


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
    """A batch and its files: what Add to library would add now, everything awaiting review, files still going (U7.1).

    ready_to_add counts processed candidates not marked as duplicates; pending_review counts every candidate of a
    finished file awaiting commit — duplicates and rejections included, which a commit discards.
    """

    batch_id: uuid.UUID
    files: list[FileStatusOut]
    ready_to_add: int
    pending_review: int
    in_progress: int


class DuplicateOfOut(BaseModel):
    """The library document a candidate duplicates (U7.2)."""

    document_id: uuid.UUID
    title: str
    kind: str


class StagedDocumentOut(BaseModel):
    """One candidate (R14.2, U7.2); the staging s3_key stays internal."""

    staged_id: uuid.UUID
    source_file_id: uuid.UUID
    source_entry_name: str | None
    entry_index: int | None
    file_name: str
    status: str
    reject_reason: str | None
    content_hash: str | None
    duplicate_of: DuplicateOfOut | None


class StagedOut(BaseModel):
    """A batch's candidates."""

    batch_id: uuid.UUID
    staged: list[StagedDocumentOut]


class UploadConfigOut(BaseModel):
    """What the client may upload — the ONE source of truth for its file checks (D2 changes only the settings)."""

    allowed_top_level_ext: list[str]
    allowed_zip_entry_ext: list[str]
    max_upload_bytes: int
    max_zip_folder_depth: int


class SkippedOut(BaseModel):
    """A candidate the commit discarded, and why."""

    staged_id: uuid.UUID
    file_name: str
    reason: str


class CommitOut(BaseModel):
    """What a commit did (U8.2): how many were added, which were skipped and why, how many files remain."""

    added: int
    skipped: list[SkippedOut]
    still_in_progress: int
    documents: list[uuid.UUID]


# ── The Library — /api/v1/library/* (routers/library.py) ──────────────────────────────────────────────────────────


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


# ── The workers' API — /internal/* (routers/internal.py) ──────────────────────────────────────────────────────────


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
    content_hash: str | None = Field(default=None, pattern="^[0-9a-f]{64}$")     # SHA-256, lowercase hex (U5.2)


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


class CandidateStagedOut(BaseModel):
    """The upserted candidate's id."""

    staged_id: uuid.UUID


class StaleSweepOut(BaseModel):
    """Result of a stale sweep."""

    reset: int
    errored: int
    enqueue_failed: int


class AbandonedSweepOut(BaseModel):
    """Result of an abandoned-upload sweep."""

    cancelled: int
    abort_failed: int


class ReconcileSweepOut(BaseModel):
    """Result of a reconcile sweep."""

    requeued: int
    errored: int
    enqueue_failed: int
