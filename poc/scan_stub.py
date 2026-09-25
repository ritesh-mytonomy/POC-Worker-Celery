"""scan_stub (design.md §6.3; R13): proves the scan queue is isolated from a saturated ingest pool."""
import time
from datetime import datetime, timezone
from typing import Any

from app.logging import get_logger
from poc.constants import TASK_SCAN_STUB
from workers.celery_app import app

log = get_logger(__name__)


@app.task(bind=True, name=TASK_SCAN_STUB, ignore_result=True)
def scan_stub(self: Any, seconds: float, enqueued_at: str) -> float:
    """Log scan_started with its start delay since enqueued_at, sleep `seconds`, log scan_finished; return the delay."""
    started = datetime.now(timezone.utc)
    delay = (started - datetime.fromisoformat(enqueued_at)).total_seconds()
    log.info("scan_started", task_id=self.request.id, enqueued_at=enqueued_at,
             started_at=started.isoformat(timespec="milliseconds"), start_delay_s=round(delay, 3))
    time.sleep(seconds)
    log.info("scan_finished", task_id=self.request.id, seconds=seconds)
    return delay
