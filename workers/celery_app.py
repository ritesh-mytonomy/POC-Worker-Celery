"""The workers' Celery app (design.md §7): delivery guarantees, routes, and beat for the sweepers."""
from typing import Any

import celery.apps.worker
from celery import Celery, signals

from app.config import get_settings
from app.constants import QUEUE_INGEST, QUEUE_MAINTENANCE, QUEUE_SCAN, TASK_RECONCILE_SWEEP, TASK_STALE_SWEEP
from app.logging import configure_logging, get_logger, say_json

configure_logging()
log = get_logger(__name__)
# Celery prints shutdown/restart notices from its signal handlers via safe_say, straight to
# stdout as plain text; no signal fires first. Replace it so those lines are JSON too (R15.1).
celery.apps.worker.safe_say = say_json


@signals.setup_logging.connect
def _keep_json_logging(**_: Any) -> None:
    """Stop Celery replacing our JSON logging with its own format."""
    configure_logging()


@signals.celeryd_init.connect
def _log_start_instead_of_banner(sender: str, instance: Any, options: dict[str, Any], **_: Any) -> None:
    """Suppress the plain-text startup banner and log its key facts as JSON."""
    instance.quiet = True
    log.info("worker_starting", hostname=sender,
             queues=options.get("queues"), concurrency=options.get("concurrency"))


settings = get_settings()

app = Celery("clinsync", broker=settings.REDIS_URL,
             include=["workers.ingest", "workers.sweepers"])   # task modules to register

app.conf.update(
    task_acks_late=True,                 # R9.1 — ack after the body, not on receipt
    task_reject_on_worker_lost=True,     # R9.2 — killed child → message back on the queue
    worker_prefetch_multiplier=1,        # R9.3 — one message per process
    task_ignore_result=True,             # state is in PostgreSQL, not a result backend
    task_soft_time_limit=settings.TASK_SOFT_TIME_LIMIT,
    task_time_limit=settings.TASK_TIME_LIMIT,
    broker_transport_options={"visibility_timeout": settings.VISIBILITY_TIMEOUT},  # R9.4
    broker_connection_retry_on_startup=True,
    task_default_queue=QUEUE_INGEST,
    task_routes={
        "workers.ingest.*":   {"queue": QUEUE_INGEST},
        "workers.scan.*":     {"queue": QUEUE_SCAN},
        "workers.sweepers.*": {"queue": QUEUE_MAINTENANCE},
    },
    # Each sweep message expires after one interval: if the consumer is stuck (or beat runs separately, as in
    # AWS), stale sweeps are discarded on receipt instead of all running at once afterwards (task 11.1).
    beat_schedule={
        "stale-sweep":     {"task": TASK_STALE_SWEEP, "schedule": settings.SWEEP_INTERVAL_SECONDS,
                            "options": {"expires": settings.SWEEP_INTERVAL_SECONDS}},
        "reconcile-sweep": {"task": TASK_RECONCILE_SWEEP, "schedule": settings.SWEEP_INTERVAL_SECONDS,
                            "options": {"expires": settings.SWEEP_INTERVAL_SECONDS}},
    },
    beat_schedule_filename="/tmp/celerybeat-schedule",   # /srv is not writable by the app user
)
try:
    settings.validate()                  # R9.5 — refuse to start on a bad combination
except ValueError as exc:
    log.error("invalid_configuration", error=str(exc))
    raise SystemExit(1) from exc
