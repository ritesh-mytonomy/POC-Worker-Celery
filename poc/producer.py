"""Publishing POC-only tasks from the API process. Imports app/ only — never workers/ (design.md §6.2a)."""
from app import tasks_client
from app.constants import QUEUE_SCAN
from poc.constants import TASK_SCAN_STUB


def enqueue_scan_stub(seconds: float, enqueued_at: str) -> str:
    """Publish scan_stub on clinsync.scan (R13); return the task id. Raises if Redis is unreachable."""
    producer = tasks_client.get_producer()
    try:
        return producer.send_task(TASK_SCAN_STUB, kwargs={"seconds": seconds, "enqueued_at": enqueued_at},
                                  queue=QUEUE_SCAN).id
    except Exception:
        tasks_client.reset_producer()
        raise
