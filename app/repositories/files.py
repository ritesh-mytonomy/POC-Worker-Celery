"""upload_file repository: claim and the fenced writes that follow it (design.md §6.2)."""
import uuid
from dataclasses import dataclass
from pathlib import PurePosixPath

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.errors import ClaimSuperseded, InvalidInput
from app.logging import get_logger

log = get_logger(__name__)

TERMINAL_STATUSES = frozenset({"processed", "partial", "rejected", "error"})
STATUS_MESSAGE_MAX = 512                 # upload_file.status_message is VARCHAR(512)

# Every write after claim is fenced on both the token and the status (R4.4).
_FENCE = "WHERE file_id = :file_id AND claim_token = :token AND status = 'processing'"
_PROGRESS_FIELDS = ("entries_total", "entries_done", "detected_type")

# design.md §6.2 — atomic, one statement (R3.2, R3.3, R3.5).
_CLAIM_SQL = text("""
UPDATE upload_file
SET status = 'processing',
    attempt_count = attempt_count + 1,
    heartbeat_at  = now(),
    claim_token   = gen_random_uuid(),
    updated_at    = now()
WHERE file_id = :file_id
  AND attempt_count < :max_attempts
  AND ( status = 'uploaded'
        OR (status = 'processing' AND heartbeat_at < now() - make_interval(secs => :stale)) )
RETURNING file_id, organization_id, batch_id, s3_key, file_name, file_ext,
          is_archive, entries_done, attempt_count, claim_token;
""")


@dataclass(frozen=True)
class FileClaim:
    """A successful claim: the file's identity plus the token every later write must carry."""

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


def claim(session: Session, file_id: uuid.UUID, *, max_attempts: int, stale_after_seconds: int) -> FileClaim | None:
    """Claim the file for one worker and commit; return None if it is not claimable.

    Must be the first statement in its session's transaction: now() is fixed at transaction
    start, so an older transaction would stamp an already-aged heartbeat_at and judge staleness
    against a past instant.
    """
    row = session.execute(
        _CLAIM_SQL, {"file_id": file_id, "max_attempts": max_attempts, "stale": stale_after_seconds}
    ).mappings().one_or_none()
    session.commit()
    return FileClaim(**row) if row is not None else None


def _fenced_update(session: Session, file_id: uuid.UUID, token: uuid.UUID, set_clause: str,
                   params: dict[str, object], returning: str = "file_id") -> dict[str, object]:
    """Run one fenced UPDATE; roll back and raise ClaimSuperseded if it matched no row."""
    row = session.execute(
        text(f"UPDATE upload_file SET {set_clause} {_FENCE} RETURNING {returning}"),
        {**params, "file_id": file_id, "token": token},
    ).mappings().one_or_none()
    if row is None:
        session.rollback()
        raise ClaimSuperseded(file_id)
    return dict(row)


def heartbeat(session: Session, file_id: uuid.UUID, token: uuid.UUID) -> None:
    """Refresh the heartbeat of a file this token owns."""
    _fenced_update(session, file_id, token, "heartbeat_at = now(), updated_at = now()", {})
    session.commit()


def progress(session: Session, file_id: uuid.UUID, token: uuid.UUID, *, entries_total: int | None = None,
             entries_done: int | None = None, detected_type: str | None = None) -> None:
    """Set only the progress fields passed (None means not passed), and refresh the heartbeat."""
    values = {"entries_total": entries_total, "entries_done": entries_done, "detected_type": detected_type}
    passed = {name: values[name] for name in _PROGRESS_FIELDS if values[name] is not None}
    if not passed:
        raise InvalidInput("progress() needs at least one of entries_total, entries_done, detected_type")
    assignments = "".join(f"{name} = :{name}, " for name in passed)   # names from the fixed allow-list
    _fenced_update(session, file_id, token, f"{assignments}heartbeat_at = now(), updated_at = now()", passed)
    session.commit()


def truncate_message(message: str | None) -> str | None:
    """Fit a status message into VARCHAR(512), ending with "…" when cut."""
    if message is None or len(message) <= STATUS_MESSAGE_MAX:
        return message
    return message[: STATUS_MESSAGE_MAX - 1] + "…"


def finish(session: Session, file_id: uuid.UUID, token: uuid.UUID, status: str,
           status_message: str | None = None) -> None:
    """Set a terminal status and message and clear the claim token (R10.3)."""
    if status not in TERMINAL_STATUSES:
        raise InvalidInput(f"finish() needs a terminal status, got {status!r}")
    _fenced_update(
        session, file_id, token,
        "status = :status, status_message = :status_message, claim_token = NULL, updated_at = now()",
        {"status": status, "status_message": truncate_message(status_message)},
        returning="file_id, is_archive",
    )
    # Task 9.3 (design.md §8.5a): rejecting an archive also rejects its `processed` candidates.
    # That statement goes here — after the fenced UPDATE has locked the row, before the commit.
    session.commit()


def release(session: Session, file_id: uuid.UUID, token: uuid.UUID, reason: str) -> None:
    """Hand the file back to `uploaded` before a retry; reset uploaded_at so reconcile waits (rev 1.2)."""
    row = _fenced_update(
        session, file_id, token,
        "status = 'uploaded', claim_token = NULL, uploaded_at = now(), updated_at = now()",
        {}, returning="file_id, attempt_count",
    )
    session.commit()
    log.info("released", file_id=str(file_id), attempt=row["attempt_count"], reason=reason)


@dataclass(frozen=True)
class SeededFile:
    """A file row created by seed_batch."""

    file_id: uuid.UUID
    file_name: str
    is_archive: bool
    status: str


def file_ext_of(file_name: str) -> str:
    """Lower-case extension without the dot ('' when there is none)."""
    return PurePosixPath(file_name).suffix.lower().lstrip(".")


def seed_batch(session: Session, organization_id: uuid.UUID, files: list[tuple[str, str]],
               allowed_ext: list[str]) -> tuple[uuid.UUID, list[SeededFile]]:
    """POC only: create a batch and `uploading` file rows for (file_name, s3_key) pairs already in incoming/."""
    if not files:
        raise InvalidInput("files must not be empty")
    for file_name, s3_key in files:
        if file_ext_of(file_name) not in allowed_ext:
            raise InvalidInput(f"{file_name!r}: extension not allowed (allowed: {', '.join(allowed_ext)})")
        if not s3_key.startswith("ClinSync/incoming/"):
            raise InvalidInput(f"{s3_key!r}: s3_key must be under ClinSync/incoming/")
    batch_id = session.execute(
        text("INSERT INTO upload_batch (organization_id) VALUES (:org) RETURNING batch_id"), {"org": organization_id}
    ).scalar_one()
    seeded = []
    for file_name, s3_key in files:
        ext = file_ext_of(file_name)
        row = session.execute(text("""
            INSERT INTO upload_file (batch_id, organization_id, file_name, file_ext, is_archive, s3_key)
            VALUES (:batch_id, :org, :file_name, :ext, :is_archive, :s3_key)
            RETURNING file_id, file_name, is_archive, status"""),
            {"batch_id": batch_id, "org": organization_id, "file_name": file_name, "ext": ext,
             "is_archive": ext == "zip", "s3_key": s3_key}).mappings().one()
        seeded.append(SeededFile(**row))
    session.commit()
    return batch_id, seeded


def file_exists(session: Session, file_id: uuid.UUID) -> tuple[uuid.UUID, str] | None:
    """Return (organization_id, status) of the file, or None if it does not exist."""
    row = session.execute(text("SELECT organization_id, status FROM upload_file WHERE file_id = :id"),
                          {"id": file_id}).one_or_none()
    session.rollback()                   # read-only; end the transaction
    return (row.organization_id, row.status) if row else None


_CONFIRM_SQL = text("""
UPDATE upload_file
SET status = 'uploaded', uploaded_at = now(), updated_at = now()
WHERE file_id = :file_id AND status = 'uploading'
RETURNING organization_id
""")


@dataclass(frozen=True)
class ConfirmResult:
    """Outcome of confirm_upload: the status now, and whether this call made the transition."""

    status: str
    organization_id: uuid.UUID
    transitioned: bool


def confirm_upload(session: Session, file_id: uuid.UUID) -> ConfirmResult | None:
    """Move uploading → uploaded in one conditional UPDATE and commit; None if the file does not exist (R2.1, R2.5)."""
    row = session.execute(_CONFIRM_SQL, {"file_id": file_id}).one_or_none()
    if row is not None:
        session.commit()
        return ConfirmResult("uploaded", row.organization_id, transitioned=True)
    # Already past uploading (or unknown): read the current status for the response only; nothing is written.
    current = session.execute(text("SELECT status, organization_id FROM upload_file WHERE file_id = :id"),
                              {"id": file_id}).one_or_none()
    session.rollback()
    return ConfirmResult(current.status, current.organization_id, transitioned=False) if current else None


def batch_exists(session: Session, batch_id: uuid.UUID) -> bool:
    """True if the batch exists."""
    return session.execute(text("SELECT 1 FROM upload_batch WHERE batch_id = :b"), {"b": batch_id}).first() is not None


def list_batch_files(session: Session, batch_id: uuid.UUID) -> list[dict[str, object]] | None:
    """Every file of the batch with its status fields (R14.1), oldest first then by name; None if no such batch."""
    if not batch_exists(session, batch_id):
        return None
    rows = session.execute(text("""
        SELECT file_id, file_name, status, entries_total, entries_done, attempt_count, detected_type, status_message
        FROM upload_file WHERE batch_id = :b ORDER BY created_at, file_name, file_id"""), {"b": batch_id}).mappings()
    return [dict(r) for r in rows]
