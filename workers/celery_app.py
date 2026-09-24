"""Celery app — minimal stub for Checkpoint A; replaced by task 6.1 (design.md §7)."""
from typing import Any

import celery.apps.worker
from celery import Celery, signals

from app.config import get_settings
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

app = Celery("clinsync", broker=settings.REDIS_URL)
app.conf.broker_connection_retry_on_startup = True
try:
    settings.validate()                  # R9.5 — refuse to start on a bad combination
except ValueError as exc:
    log.error("invalid_configuration", error=str(exc))
    raise SystemExit(1) from exc
