"""Row helpers shared by the repository tests. Setup helpers commit, so a later rollback cannot undo them."""
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models import UploadBatch, UploadFile


def make_file(session: Session, status: str = "uploaded", attempt_count: int = 0) -> uuid.UUID:
    """Insert a batch and one file with the given status and attempt count; return file_id."""
    org = uuid.uuid4()
    batch = UploadBatch(organization_id=org)
    session.add(batch)
    session.flush()
    upload = UploadFile(batch_id=batch.batch_id, organization_id=org, file_name="a.docx", file_ext="docx",
                        is_archive=False, s3_key="ClinSync/incoming/a.docx", status=status,
                        attempt_count=attempt_count)
    session.add(upload)
    session.commit()
    return upload.file_id


def set_heartbeat_age(session: Session, file_id: uuid.UUID, age_seconds: int) -> None:
    """Mark the file processing with a heartbeat `age_seconds` old, by backdating it in the database."""
    session.execute(
        text("UPDATE upload_file SET status = 'processing', heartbeat_at = now() - make_interval(secs => :age) "
             "WHERE file_id = :id"),
        {"age": age_seconds, "id": file_id},
    )
    session.commit()


def backdate_timestamps(session: Session, file_id: uuid.UUID, seconds: int = 60) -> None:
    """Age heartbeat_at, updated_at and uploaded_at, so a write stamping now() visibly changes them."""
    session.execute(
        text("UPDATE upload_file SET heartbeat_at = now() - make_interval(secs => :s), "
             "updated_at = now() - make_interval(secs => :s), uploaded_at = now() - make_interval(secs => :s) "
             "WHERE file_id = :id"),
        {"s": seconds, "id": file_id},
    )
    session.commit()


def db_row(session: Session, file_id: uuid.UUID) -> Any:
    """Read the file's status, attempt_count and claim_token."""
    return session.execute(
        text("SELECT status, attempt_count, claim_token FROM upload_file WHERE file_id = :id"), {"id": file_id}
    ).one()


def full_row(session: Session, file_id: uuid.UUID) -> dict[str, Any]:
    """Read every column of the file as a dict."""
    return dict(session.execute(text("SELECT * FROM upload_file WHERE file_id = :id"), {"id": file_id})
                .mappings().one())


def changed(before: dict[str, Any], after: dict[str, Any]) -> set[str]:
    """Names of the columns whose values differ."""
    return {name for name in before if before[name] != after[name]}
