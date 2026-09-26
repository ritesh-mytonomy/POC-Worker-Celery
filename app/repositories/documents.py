"""documents repository: the library (upload-ingest-merge design.md §3, U6)."""
import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.naming import file_name_norm


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
                   title_norm: str) -> Duplicate | None:
    """The library document with this content — checked first — or else this title, in the organization (U6.2)."""
    row = session.execute(_FIND_DUPLICATE_SQL, {"org": organization_id, "hash": content_hash,
                                                "title_norm": title_norm}).one_or_none()
    return Duplicate(row.document_id, row.kind) if row else None
