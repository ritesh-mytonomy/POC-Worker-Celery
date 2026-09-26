"""staged_document repository: the fenced candidate upsert (design.md §6.2; R8.3)."""
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.errors import CandidateIdentityMismatch, ClaimSuperseded, InvalidInput
from app.naming import title_norm, title_of
from app.repositories import documents
from app.repositories.files import batch_exists, truncate_message

CANDIDATE_STATUSES = frozenset({"processed", "rejected"})

# Locks the parent row until commit: a concurrent takeover (claim's UPDATE) waits for us, or we
# wait for it and then see its new token. A plain SELECT would not block — the INSERT's foreign-key
# check only takes FOR KEY SHARE, which a claim UPDATE (no key columns changed) does not conflict with.
_LOCK_PARENT_SQL = text("""
SELECT batch_id, organization_id
FROM upload_file
WHERE file_id = :file_id AND claim_token = :token AND status = 'processing'
FOR UPDATE
""")

_UPSERT_SQL = text("""
INSERT INTO staged_document
  (batch_id, organization_id, source_file_id, source_entry_name, entry_index,
   file_name, file_ext, size_bytes, content_hash, s3_key, status, reject_reason,
   proposed_title, title_norm, duplicate_of_document_id, duplicate_kind)
VALUES
  (:batch_id, :organization_id, :file_id, :source_entry_name, :entry_index,
   :file_name, :file_ext, :size_bytes, :content_hash, :s3_key, :status, :reject_reason,
   :proposed_title, :title_norm, :duplicate_of, :duplicate_kind)
ON CONFLICT ON CONSTRAINT uq_entry DO UPDATE
SET status = EXCLUDED.status, s3_key = EXCLUDED.s3_key,
    reject_reason = EXCLUDED.reject_reason, size_bytes = EXCLUDED.size_bytes,
    content_hash = EXCLUDED.content_hash, proposed_title = EXCLUDED.proposed_title,
    title_norm = EXCLUDED.title_norm, duplicate_of_document_id = EXCLUDED.duplicate_of_document_id,
    duplicate_kind = EXCLUDED.duplicate_kind, updated_at = now()
RETURNING staged_id, entry_index, file_name, file_ext
""")


def _validate(status: str, s3_key: str | None, reject_reason: str | None,
              source_entry_name: str | None, entry_index: int | None, content_hash: str | None) -> None:
    """Raise InvalidInput for a candidate the schema would accept but that makes no sense."""
    if status not in CANDIDATE_STATUSES:
        raise InvalidInput(f"candidate status must be processed or rejected, got {status!r}")
    if status == "processed" and (not s3_key or reject_reason):
        raise InvalidInput("a processed candidate needs an s3_key and no reject_reason")
    if status == "processed" and not content_hash:          # documents.content_hash is NOT NULL (U5.2)
        raise InvalidInput("a processed candidate needs its content_hash")
    if status == "rejected" and (s3_key or not reject_reason):
        raise InvalidInput("a rejected candidate needs a reject_reason and no s3_key")
    if (source_entry_name is None) != (entry_index is None):
        raise InvalidInput("source_entry_name and entry_index must both be set (archive entry) or both be None")


def upsert(session: Session, file_id: uuid.UUID, token: uuid.UUID, *, source_entry_name: str | None,
           entry_index: int | None, file_name: str, file_ext: str, status: str, size_bytes: int | None = None,
           s3_key: str | None = None, reject_reason: str | None = None,
           content_hash: str | None = None) -> uuid.UUID:
    """Insert or overwrite the file's candidate for this entry, fenced on the claim token; return staged_id.

    batch_id and organization_id are taken from the locked parent row, never from the caller. An overwrite
    whose entry_index, file_name or file_ext differs from the stored row raises CandidateIdentityMismatch.
    """
    _validate(status, s3_key, reject_reason, source_entry_name, entry_index, content_hash)
    parent = session.execute(_LOCK_PARENT_SQL, {"file_id": file_id, "token": token}).one_or_none()
    if parent is None:
        session.rollback()
        raise ClaimSuperseded(file_id)
    # U6.2 — the title every candidate proposes, and for a processed one, a library document it duplicates:
    # same content first, then same title. Recomputed on every replay.
    proposed_title = title_of(file_name)
    normalized_title = title_norm(proposed_title)
    duplicate = (documents.find_duplicate(session, parent.organization_id, content_hash, normalized_title)
                 if status == "processed" else None)
    stored = session.execute(_UPSERT_SQL, {
        "batch_id": parent.batch_id, "organization_id": parent.organization_id, "file_id": file_id,
        "source_entry_name": source_entry_name, "entry_index": entry_index, "file_name": file_name,
        "file_ext": file_ext, "size_bytes": size_bytes, "content_hash": content_hash, "s3_key": s3_key,
        "status": status,
        "reject_reason": truncate_message(reject_reason),
        "proposed_title": proposed_title, "title_norm": normalized_title,
        "duplicate_of": duplicate.document_id if duplicate else None,
        "duplicate_kind": duplicate.kind if duplicate else None,
    }).one()
    # A replay must reproduce the identity columns exactly (they are not overwritten on conflict);
    # a mismatch would orphan the first call's staging object, so undo the overwrite and refuse.
    passed = (entry_index, file_name, file_ext)
    if (stored.entry_index, stored.file_name, stored.file_ext) != passed:
        session.rollback()
        raise CandidateIdentityMismatch(
            f"candidate {source_entry_name!r} of file {file_id} is stored as "
            f"{(stored.entry_index, stored.file_name, stored.file_ext)!r}, replay passed {passed!r}"
        )
    session.commit()
    return stored.staged_id


def list_for_batch(session: Session, batch_id: uuid.UUID) -> list[dict[str, object]] | None:
    """Every candidate of the batch (R14.2): per file, the direct upload first, then entries in order."""
    if not batch_exists(session, batch_id):
        return None
    rows = session.execute(text("""
        SELECT staged_id, source_file_id, source_entry_name, entry_index, file_name, status, reject_reason
        FROM staged_document WHERE batch_id = :b
        ORDER BY source_file_id, entry_index NULLS FIRST, source_entry_name"""), {"b": batch_id}).mappings()
    return [dict(r) for r in rows]


# ── Commit (upload-ingest-merge U8, design.md §5.4) ─────────────────────────

def of_finished_files(session: Session, batch_id: uuid.UUID) -> list[Any]:
    """The candidates a commit may act on: only those of FINISHED files, by file then entry position."""
    return list(session.execute(text("""
        SELECT c.staged_id, c.source_file_id, c.file_name, c.file_ext, c.size_bytes, c.content_hash, c.s3_key,
               c.status, c.proposed_title, c.title_norm
        FROM staged_document c JOIN upload_file f ON f.file_id = c.source_file_id
        WHERE c.batch_id = :b AND f.status IN ('processed', 'partial', 'rejected', 'error')
        ORDER BY f.created_at, f.file_id, c.entry_index NULLS FIRST, c.source_entry_name"""), {"b": batch_id}).all())


def resolve_and_delete(session: Session, staged_id: uuid.UUID, resolution: str | None,
                       confirmed_title: str | None) -> None:
    """Record the commit's decision on the candidate, then delete it: the library row is the record (D12)."""
    if resolution is not None:
        session.execute(text("UPDATE staged_document SET resolution = :r, confirmed_title = :t WHERE staged_id = :id"),
                        {"r": resolution, "t": confirmed_title, "id": staged_id})
    session.execute(text("DELETE FROM staged_document WHERE staged_id = :id"), {"id": staged_id})


def any_left(session: Session, batch_id: uuid.UUID) -> bool:
    """True while the batch still has candidates (of unfinished files, or not yet committed)."""
    return session.execute(text("SELECT 1 FROM staged_document WHERE batch_id = :b LIMIT 1"),
                           {"b": batch_id}).first() is not None
