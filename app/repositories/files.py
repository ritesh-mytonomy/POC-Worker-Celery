"""upload_file repository: claim now; fenced writes follow in task 3.3 (design.md §6.2)."""
import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.orm import Session

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
