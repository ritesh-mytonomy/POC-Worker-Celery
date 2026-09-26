"""documents repository: the library (upload-ingest-merge design.md §3, U6)."""
import uuid

from sqlalchemy import text
from sqlalchemy.orm import Session


def normalize_file_name(file_name: str) -> str:
    """documents.file_name_norm: trimmed and casefolded — the name half of the name+size duplicate check."""
    return file_name.strip().casefold()


def find_by_name_size(session: Session, organization_id: uuid.UUID, file_name: str,
                      size_bytes: int) -> uuid.UUID | None:
    """The library document with this name (case-insensitive) and size, if any (U1.4, U6.1)."""
    return session.execute(text("""
        SELECT document_id FROM documents
        WHERE organization_id = :org AND file_name_norm = :name AND size_bytes = :size
        LIMIT 1"""), {"org": organization_id, "name": normalize_file_name(file_name), "size": size_bytes}
    ).scalar_one_or_none()
