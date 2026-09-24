"""SQLAlchemy models mirroring infra/postgres/init.sql (design.md §4). init.sql owns the schema."""
import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Declarative base for all models."""


class UploadBatch(Base):
    """One user upload session, grouping its files."""

    __tablename__ = "upload_batch"
    __table_args__ = (
        CheckConstraint("status IN ('in_progress','staged','committed','abandoned')", name="upload_batch_status_check"),
    )

    batch_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, server_default=text("gen_random_uuid()"))
    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'in_progress'"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class UploadFile(Base):
    """One uploaded object in incoming/, and the state machine of its processing (design.md §5)."""

    __tablename__ = "upload_file"
    __table_args__ = (
        CheckConstraint(
            "status IN ('uploading','uploaded','processing','processed','partial','rejected','error')",
            name="upload_file_status_check",
        ),
        Index("idx_file_batch", "batch_id"),
        Index("idx_file_sweep", "status", "heartbeat_at"),
        Index("idx_file_reconcile", "status", "uploaded_at"),
    )

    file_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, server_default=text("gen_random_uuid()"))
    batch_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("upload_batch.batch_id", ondelete="CASCADE"), nullable=False
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    file_name: Mapped[str] = mapped_column(String(512), nullable=False)
    file_ext: Mapped[str] = mapped_column(String(10), nullable=False)
    is_archive: Mapped[bool] = mapped_column(Boolean, nullable=False)
    s3_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'uploading'"))
    status_message: Mapped[str | None] = mapped_column(String(512))
    detected_type: Mapped[str | None] = mapped_column(String(32))
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
        CheckConstraint("status IN ('processed','rejected')", name="staged_document_status_check"),
        # R8.3 — a replayed entry overwrites; NULLS NOT DISTINCT binds direct uploads too.
        UniqueConstraint(
            "source_file_id", "source_entry_name", name="uq_entry", postgresql_nulls_not_distinct=True
        ),
    )

    staged_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, server_default=text("gen_random_uuid()"))
    batch_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("upload_batch.batch_id", ondelete="CASCADE"), nullable=False
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    source_file_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("upload_file.file_id", ondelete="CASCADE"), nullable=False
    )
    source_entry_name: Mapped[str | None] = mapped_column(String(1024))
    entry_index: Mapped[int | None] = mapped_column(Integer)
    file_name: Mapped[str] = mapped_column(String(512), nullable=False)
    file_ext: Mapped[str] = mapped_column(String(10), nullable=False)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    s3_key: Mapped[str | None] = mapped_column(String(1024))
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    reject_reason: Mapped[str | None] = mapped_column(String(512))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
