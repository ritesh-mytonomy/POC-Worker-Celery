"""upload_file repository: claim and the fenced writes that follow it (design.md §6.2)."""
import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.errors import ClaimSuperseded
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
        raise ValueError("progress() needs at least one of entries_total, entries_done, detected_type")
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
        raise ValueError(f"finish() needs a terminal status, got {status!r}")
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
