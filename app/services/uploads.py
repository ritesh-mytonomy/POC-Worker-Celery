"""Confirm an upload and hand it to the background (R2)."""
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app import tasks_client
from app.logging import get_logger
from app.repositories import files

log = get_logger(__name__)
Enqueue = Callable[[str, str], None]


@dataclass(frozen=True)
class Confirmed:
    """What confirm returns to the caller."""

    status: str
    enqueued: bool


def confirm(db: Session, file_id: uuid.UUID, enqueue: Enqueue | None = None) -> Confirmed | None:
    """staged|uploading → uploaded, commit, then enqueue once; later statuses come back unchanged. None if unknown."""
    result = files.confirm_upload(db, file_id)   # commits before returning
    if result is None:
        return None
    if not result.transitioned:
        return Confirmed(result.status, enqueued=False)
    try:
        (enqueue or tasks_client.enqueue_process_upload)(str(file_id), str(result.organization_id))
    except Exception as exc:
        # R2.6: the file stays `uploaded`; the reconcile sweeper re-enqueues it.
        log.warning("enqueue_failed", file_id=str(file_id), error=repr(exc))
        return Confirmed("uploaded", enqueued=False)
    log.info("enqueued", file_id=str(file_id))
    return Confirmed("uploaded", enqueued=True)
