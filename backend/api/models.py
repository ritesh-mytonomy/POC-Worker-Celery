"""SQLAlchemy models mirroring infra/postgres/init.sql, which is specs/upload-ingest-merge/schema.sql — the LLD's
tables (upload-ingest-merge design.md §3). The SQL file owns the schema; its set_updated_at triggers live there only.
"""
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CHAR,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Declarative base for all models."""


# ── M1 · Organizations ──────────────────────────────────────────────────────

class Organization(Base):
    """A tenant. The POC has one row, POC_ORGANIZATION_ID."""

    __tablename__ = "organizations"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, server_default=text("gen_random_uuid()"))
    name: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


# ── Reference masters (deferred: created, unused) ───────────────────────────

class Specialty(Base):
    """Clinical specialty master (deferred)."""

    __tablename__ = "specialty"

    specialty_id: Mapped[int] = mapped_column(BigInteger, Identity(always=False), primary_key=True)
    code: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))


class DocumentType(Base):
    """Document type master (deferred)."""

    __tablename__ = "document_type"

    document_type_id: Mapped[int] = mapped_column(BigInteger, Identity(always=False), primary_key=True)
    code: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))


# ── M2 · The library ────────────────────────────────────────────────────────

class Document(Base):
    """A library document; document_id is uuid5 of the candidate it came from (design.md §5.4)."""

    __tablename__ = "documents"
    __table_args__ = (
        CheckConstraint("status IN ('active', 'archived')", name="documents_status_check"),
        CheckConstraint("highest_risk IN ('HIGH', 'MEDIUM', 'LOW', 'CONFIRMATORY')",
                        name="documents_highest_risk_check"),
        UniqueConstraint("organization_id", "title_norm", name="uq_org_title"),
        Index("idx_doc_org_hash", "organization_id", "content_hash"),
        Index("idx_doc_org_filename", "organization_id", "file_name_norm"),
        Index("idx_doc_org_scanned", "organization_id", "last_scanned_at"),
    )

    document_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("organizations.id"), nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    title_norm: Mapped[str] = mapped_column(String(512), nullable=False)
    file_name: Mapped[str] = mapped_column(String(512), nullable=False)
    file_name_norm: Mapped[str] = mapped_column(String(512), nullable=False)
    document_type_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("document_type.document_type_id"))
    current_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    s3_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    file_ext: Mapped[str] = mapped_column(String(10), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    content_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    # Closes the documents ↔ upload_file cycle; the DDL adds it with ALTER TABLE, as schema.sql does.
    source_file_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("upload_file.file_id", ondelete="SET NULL", use_alter=True, name="fk_doc_source_file")
    )
    video_script_seq: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'active'"))
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    version_uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False,
                                                          server_default=func.now())
    version_uploaded_by: Mapped[int] = mapped_column(BigInteger, nullable=False)
    last_scanned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_scan_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    highest_risk: Mapped[str | None] = mapped_column(String(16))
    open_findings: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))


class DocumentSpecialty(Base):
    """Document ↔ specialty tags (deferred)."""

    __tablename__ = "document_specialty"

    document_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("documents.document_id", ondelete="CASCADE"), primary_key=True
    )
    specialty_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("specialty.specialty_id"), primary_key=True)


# ── M2 · Upload and ingest ──────────────────────────────────────────────────

class UploadBatch(Base):
    """One user upload session, grouping its files."""

    __tablename__ = "upload_batch"
    __table_args__ = (
        CheckConstraint("status IN ('in_progress', 'staged', 'committed', 'abandoned')",
                        name="upload_batch_status_check"),
        Index("idx_batch_org", "organization_id", "batch_id"),
    )

    batch_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, server_default=text("gen_random_uuid()"))
    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("organizations.id"), nullable=False)
    created_by: Mapped[int] = mapped_column(BigInteger, nullable=False)          # POC_USER_ID until auth exists
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'in_progress'"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class UploadFile(Base):
    """One object the browser sent to incoming/, and the state machine of its processing (LLD M2.5)."""

    __tablename__ = "upload_file"
    __table_args__ = (
        CheckConstraint("intent IN ('new', 'replace')", name="upload_file_intent_check"),
        CheckConstraint(
            "status IN ('staged', 'uploading', 'uploaded', 'processing', "
            "'processed', 'partial', 'rejected', 'error')",
            name="upload_file_status_check",
        ),
        CheckConstraint(
            "(intent = 'new' AND replaces_document_id IS NULL) OR "
            "(intent = 'replace' AND replaces_document_id IS NOT NULL AND is_archive = FALSE)",
            name="chk_replace_target",
        ),
        UniqueConstraint("batch_id", "client_ref", name="uq_batch_client_ref"),
        UniqueConstraint("upload_id", name="uq_upload_id"),
        Index("idx_file_batch", "batch_id"),
        Index("idx_file_sweep", "status", "heartbeat_at"),
        Index("idx_file_reconcile", "status", "uploaded_at"),
        Index("idx_file_stale", "status", "created_at"),
    )

    file_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, server_default=text("gen_random_uuid()"))
    batch_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("upload_batch.batch_id", ondelete="CASCADE"), nullable=False
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("organizations.id"), nullable=False)
    client_ref: Mapped[str] = mapped_column(String(64), nullable=False,
                                            server_default=text("gen_random_uuid()::text"))
    file_name: Mapped[str] = mapped_column(String(512), nullable=False)
    file_ext: Mapped[str] = mapped_column(String(10), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    content_type: Mapped[str | None] = mapped_column(String(128))
    content_hash: Mapped[str | None] = mapped_column(CHAR(64))
    s3_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    upload_id: Mapped[str | None] = mapped_column(String(255))                  # S3's multipart id
    is_archive: Mapped[bool] = mapped_column(Boolean, nullable=False)
    intent: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'new'"))
    replaces_document_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("documents.document_id"))
    detected_type: Mapped[str | None] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'staged'"))
    status_message: Mapped[str | None] = mapped_column(String(512))
    entries_total: Mapped[int | None] = mapped_column(Integer)
    entries_done: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    claim_token: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    uploaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class StagedDocument(Base):
    """One candidate document produced from a file: the direct upload itself or one archive entry."""

    __tablename__ = "staged_document"
    __table_args__ = (
        CheckConstraint("status IN ('processed', 'rejected')", name="staged_document_status_check"),
        CheckConstraint("duplicate_kind IN ('replace_requested', 'same_title', 'same_content')",
                        name="staged_document_duplicate_kind_check"),
        CheckConstraint("resolution IN ('pending', 'create_new', 'replace_existing', 'discard')",
                        name="staged_document_resolution_check"),
        # R8.3 — a replayed entry overwrites; NULLS NOT DISTINCT binds direct uploads too.
        UniqueConstraint("source_file_id", "source_entry_name", "video_script_seq", name="uq_entry",
                         postgresql_nulls_not_distinct=True),
        Index("idx_staged_batch_status", "batch_id", "status"),
    )

    staged_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, server_default=text("gen_random_uuid()"))
    batch_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("upload_batch.batch_id", ondelete="CASCADE"), nullable=False
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("organizations.id"), nullable=False)
    source_file_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("upload_file.file_id", ondelete="CASCADE"), nullable=False
    )
    source_entry_name: Mapped[str | None] = mapped_column(String(1024))
    entry_index: Mapped[int | None] = mapped_column(Integer)
    file_name: Mapped[str] = mapped_column(String(512), nullable=False)
    file_ext: Mapped[str] = mapped_column(String(10), nullable=False)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    content_hash: Mapped[str | None] = mapped_column(CHAR(64))
    s3_key: Mapped[str | None] = mapped_column(String(1024))
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    reject_reason: Mapped[str | None] = mapped_column(String(512))
    proposed_title: Mapped[str | None] = mapped_column(String(512))
    confirmed_title: Mapped[str | None] = mapped_column(String(512))
    title_norm: Mapped[str | None] = mapped_column(String(512))
    proposed_document_type_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("document_type.document_type_id")
    )
    confirmed_document_type_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("document_type.document_type_id")
    )
    duplicate_of_document_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("documents.document_id"))
    duplicate_kind: Mapped[str | None] = mapped_column(String(20))
    resolution: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'pending'"))
    video_script_seq: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


# ── Audit ───────────────────────────────────────────────────────────────────

class AuditLog(Base):
    """One governance event; this merge writes batch_committed (design.md §5.4)."""

    __tablename__ = "audit_log"
    __table_args__ = (Index("idx_audit_org_time", "organization_id", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, server_default=text("gen_random_uuid()"))
    organization_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("organizations.id"))
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    action: Mapped[str] = mapped_column(String, nullable=False)
    entity_type: Mapped[str | None] = mapped_column(String)
    entity_id: Mapped[str | None] = mapped_column(String)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'"))
    request_id: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
