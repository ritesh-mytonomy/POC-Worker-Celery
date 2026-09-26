"""Row helpers shared by the repository tests. Setup helpers commit, so a later rollback cannot undo them."""
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models import UploadBatch, UploadFile
from app.repositories.files import claim

MAX_ATTEMPTS = 3
STALE = 30                               # the POC value
POC_ORG = uuid.UUID("00000000-0000-4000-8000-000000000001")   # seeded by init.sql; every organization_id is an FK
POC_USER = 0                             # POC_USER_ID, the system actor
SIZE = 1234                              # upload_file.size_bytes is NOT NULL
NOT_CONFIRMED = ("staged", "uploading")


@contextmanager
def triggers_off(session: Session) -> Iterator[None]:
    """Run the block's statements with the set_updated_at triggers off, so a backdated updated_at sticks.

    A db_session test is one transaction, where now() never moves: with the trigger on, backdating updated_at
    would be overwritten by that same now(), and a later write could not visibly change it. The role is reset
    straight after, so the code under test runs with the triggers on. Needs a superuser, as the compose role is.
    """
    session.execute(text("SET LOCAL session_replication_role = replica"))
    try:
        yield
    finally:
        session.execute(text("SET LOCAL session_replication_role = origin"))


def make_file(session: Session, status: str = "uploaded", attempt_count: int = 0) -> uuid.UUID:
    """Insert a batch and one file with the given status and attempt count; return file_id.

    Files past `uploading` get an uploaded_at one hour old, as a confirmed file would have.
    """
    batch = UploadBatch(organization_id=POC_ORG, created_by=POC_USER)
    session.add(batch)
    session.flush()
    upload = UploadFile(batch_id=batch.batch_id, organization_id=POC_ORG, file_name="a.docx", file_ext="docx",
                        size_bytes=SIZE, is_archive=False, s3_key="ClinSync/incoming/a.docx", status=status,
                        attempt_count=attempt_count)
    session.add(upload)
    session.flush()
    if status not in NOT_CONFIRMED:
        session.execute(text("UPDATE upload_file SET uploaded_at = now() - interval '1 hour' WHERE file_id = :id"),
                        {"id": upload.file_id})
    session.commit()
    return upload.file_id


def claimed(session: Session) -> tuple[uuid.UUID, uuid.UUID]:
    """Create and claim a file; return (file_id, claim_token)."""
    file_id = make_file(session)
    result = claim(session, file_id, max_attempts=MAX_ATTEMPTS, stale_after_seconds=STALE)
    assert result is not None
    return file_id, result.claim_token


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
    with triggers_off(session):
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


def set_uploaded_age(session: Session, file_id: uuid.UUID, age_seconds: int) -> None:
    """Set uploaded_at to `age_seconds` ago, by backdating it in the database."""
    session.execute(text("UPDATE upload_file SET uploaded_at = now() - make_interval(secs => :age) WHERE file_id = :id"),
                    {"age": age_seconds, "id": file_id})
    session.commit()
