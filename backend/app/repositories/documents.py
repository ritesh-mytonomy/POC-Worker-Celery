"""documents repository: the library (upload-ingest-merge design.md §3, U6)."""
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.naming import file_name_norm, title_norm


def find_by_name_size(session: Session, organization_id: uuid.UUID, file_name: str,
                      size_bytes: int) -> uuid.UUID | None:
    """The library document with this name (case-insensitive) and size, if any (U1.4, U6.1)."""
    return session.execute(text("""
        SELECT document_id FROM documents
        WHERE organization_id = :org AND file_name_norm = :name AND size_bytes = :size
        LIMIT 1"""), {"org": organization_id, "name": file_name_norm(file_name), "size": size_bytes}
    ).scalar_one_or_none()


@dataclass(frozen=True)
class Duplicate:
    """A library document a candidate duplicates, and how: same_content or same_title."""

    document_id: uuid.UUID
    kind: str


_FIND_DUPLICATE_SQL = text("""
    SELECT document_id, 'same_content' AS kind, 0 AS rank FROM documents
    WHERE organization_id = :org AND content_hash = :hash
    UNION ALL
    SELECT document_id, 'same_title' AS kind, 1 AS rank FROM documents
    WHERE organization_id = :org AND title_norm = :title_norm
    ORDER BY rank LIMIT 1""")


def find_duplicate(session: Session, organization_id: uuid.UUID, content_hash: str | None,
                   normalized_title: str) -> Duplicate | None:
    """The library document with this content — checked first — or else this title, in the organization (U6.2)."""
    row = session.execute(_FIND_DUPLICATE_SQL, {"org": organization_id, "hash": content_hash,
                                                "title_norm": normalized_title}).one_or_none()
    return Duplicate(row.document_id, row.kind) if row else None



# ── Commit (design.md §5.4) ─────────────────────────────────────────────────

# document_id = uuid5(DOCUMENT_NAMESPACE, staged_id): a re-run of a commit that crashed after its copies derives
# the same id — so the same processed/ key — and overwrites the same object instead of orphaning it.
DOCUMENT_NAMESPACE = uuid.UUID("5b0e6c1e-6f1d-5c8a-9d0e-2a4c1b7e9f30")


def document_id_for(staged_id: uuid.UUID) -> uuid.UUID:
    """The library document id a candidate becomes."""
    return uuid.uuid5(DOCUMENT_NAMESPACE, str(staged_id))


def document_key(organization_id: uuid.UUID, document_id: uuid.UUID, file_name: str) -> str:
    """ClinSync/processed/{org}/{document_id}/v1_{file_name} (version 1: replace is out of scope)."""
    return f"ClinSync/processed/{organization_id}/{document_id}/v1_{file_name}"


def lock_organization(session: Session, organization_id: uuid.UUID) -> None:
    """pg_advisory_xact_lock for the organization, held until the transaction ends: one commit per org at a time."""
    session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:org, 0))"), {"org": str(organization_id)})


def insert(session: Session, *, document_id: uuid.UUID, organization_id: uuid.UUID, title: str, file_name: str,
           file_ext: str, size_bytes: int, content_hash: str, s3_key: str, source_file_id: uuid.UUID,
           uploaded_by: int) -> None:
    """Insert a version-1 library document; titles and names normalised by app.naming. uq_org_title may raise."""
    session.execute(text("""
        INSERT INTO documents (document_id, organization_id, title, title_norm, file_name, file_name_norm, s3_key,
                               file_ext, size_bytes, content_hash, source_file_id, version_uploaded_by)
        VALUES (:id, :org, :title, :title_norm, :file_name, :file_name_norm, :key, :ext, :size, :hash, :source,
                :by)"""),
        {"id": document_id, "org": organization_id, "title": title, "title_norm": title_norm(title),
         "file_name": file_name, "file_name_norm": file_name_norm(file_name), "key": s3_key, "ext": file_ext,
         "size": size_bytes, "hash": content_hash, "source": source_file_id, "by": uploaded_by})



# ── The Library (U9) ────────────────────────────────────────────────────────

_LIBRARY_COLUMNS = "document_id, title, file_name, file_ext, size_bytes, added_at AS created_at, s3_key"


def list_library(session: Session, organization_id: uuid.UUID) -> list[dict[str, Any]]:
    """The organization's documents, newest first; title_norm, then document_id, break ties — a commit gives all
    its documents the same added_at."""
    rows = session.execute(text(f"""
        SELECT {_LIBRARY_COLUMNS} FROM documents WHERE organization_id = :org
        ORDER BY added_at DESC, title_norm, document_id"""), {"org": organization_id}).mappings()
    return [dict(r) for r in rows]


def get(session: Session, organization_id: uuid.UUID, document_id: uuid.UUID) -> dict[str, Any] | None:
    """One of the organization's documents, or None."""
    row = session.execute(text(f"SELECT {_LIBRARY_COLUMNS} FROM documents WHERE organization_id = :org "
                               "AND document_id = :id"), {"org": organization_id, "id": document_id}).mappings()
    found = row.one_or_none()
    return dict(found) if found else None
