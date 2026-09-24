"""The API's producer-only Celery client (design.md §6.2a). Never imports workers.celery_app."""
from functools import lru_cache

import kombu.pools
from celery import Celery

from app.config import get_settings
from app.constants import QUEUE_INGEST, QUEUE_SCAN, TASK_PROCESS_UPLOAD, TASK_SCAN_STUB


@lru_cache
def get_producer() -> Celery:
    """Build the producer on first use: fail fast when Redis is down, the reconcile sweeper recovers (R2.4)."""
    producer = Celery("clinsync-api", broker=get_settings().REDIS_URL)
    producer.conf.update(
        task_publish_retry=False,
        broker_connection_retry=False,
        broker_connection_timeout=1,
        # max_retries=0: Kombu otherwise retries opening the connection (2 s default interval) until
        # broker_connection_timeout, so a stopped Redis took ~2.3 s (rev 1.3). Producer only — workers keep
        # Kombu's retries so they reconnect when Redis comes back.
        broker_transport_options={"socket_connect_timeout": 1, "socket_timeout": 1, "max_retries": 0},
    )
    return producer


def enqueue_process_upload(file_id: str, organization_id: str) -> None:
    """Publish process_upload for one file. Raises if Redis is unreachable."""
    producer = get_producer()
    try:
        producer.send_task(TASK_PROCESS_UPLOAD, kwargs={"file_id": file_id, "organization_id": organization_id},
                           queue=QUEUE_INGEST)
    except Exception:
        reset_producer()
        raise


def enqueue_scan_stub(seconds: float, enqueued_at: str) -> str:
    """Publish scan_stub on clinsync.scan (R13); return the task id. Raises if Redis is unreachable."""
    producer = get_producer()
    try:
        return producer.send_task(TASK_SCAN_STUB, kwargs={"seconds": seconds, "enqueued_at": enqueued_at},
                                  queue=QUEUE_SCAN).id
    except Exception:
        reset_producer()
        raise


def reset_producer() -> None:
    """Discard the producer and its connection pool, so the next publish reconnects and redeclares from scratch.

    Called after every failed publish (design.md §10.2 S8). `pool.force_close_all()` alone is not enough: it closes
    the pool for good, and Kombu's process-wide registry hands the same closed pool to any new producer for the same
    broker, so every later publish failed with "Acquire on closed pool".
    """
    kombu.pools.reset()
    get_producer.cache_clear()
