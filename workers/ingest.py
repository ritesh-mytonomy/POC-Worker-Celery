"""process_upload (design.md §8.1). Skeleton so far: claim, heartbeat, finish processed — no file work yet."""
import os
from typing import Any

from app.constants import TASK_PROCESS_UPLOAD
from app.errors import ClaimSuperseded
from app.logging import get_logger
from workers.celery_app import app, settings
from workers.heartbeat import Heartbeat
from workers.internal_client import FinalStatus, InternalClient

log = get_logger(__name__)
_client: tuple[int, InternalClient] | None = None


def internal() -> InternalClient:
    """This process's Internal API client, created after fork so prefork children never share connections."""
    global _client
    if _client is None or _client[0] != os.getpid():
        _client = (os.getpid(), InternalClient.from_settings(settings))
    return _client[1]


@app.task(bind=True, max_retries=None, name=TASK_PROCESS_UPLOAD)  # attempt_count, not Celery's counter, bounds attempts
def process_upload(self: Any, file_id: str, organization_id: str) -> None:
    """Verify an uploaded file and stage whatever it contains."""
    task_id = self.request.id
    claim = internal().claim(file_id)
    if claim is None:
        log.info("claim_lost", file_id=file_id, task_id=task_id)             # R3.4 — ack and exit
        return
    log.info("claim_ok", file_id=file_id, batch_id=str(claim.batch_id), task_id=task_id,
             attempt=claim.attempt_count, claim_token=str(claim.claim_token))
    api = internal().with_token(file_id, claim.claim_token)
    beat = Heartbeat(api, every=settings.HEARTBEAT_SECONDS).start()           # R10.1
    try:
        api.finish(FinalStatus("processed"))                                  # 7.1: no file work yet
        log.info("finished", file_id=file_id, task_id=task_id, attempt=claim.attempt_count, status="processed")
    except ClaimSuperseded:
        log.warning("claim_superseded", file_id=file_id, task_id=task_id,     # another worker owns it now
                    attempt=claim.attempt_count)
    finally:
        beat.stop()
