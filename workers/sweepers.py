"""Sweeper tasks (design.md §8.7; R10.4, R11.4, R11.5): run by beat in worker-maint, executed by the API."""
import os
from typing import Any

from app.constants import TASK_RECONCILE_SWEEP, TASK_STALE_SWEEP
from app.logging import get_logger
from workers.celery_app import app, settings
from workers.internal_client import InternalClient

log = get_logger(__name__)
_client: tuple[int, InternalClient] | None = None


def internal() -> InternalClient:
    """This process's Internal API client, created after fork."""
    global _client
    if _client is None or _client[0] != os.getpid():
        _client = (os.getpid(), InternalClient.from_settings(settings))
    return _client[1]


def _sweep(kind: str, task_id: str | None) -> dict[str, int] | None:
    """Run one sweep; log its counts, or log the failure and let the next beat tick try again (no Celery retry)."""
    try:
        counts = internal().sweep(kind)
    except Exception as exc:                          # API down, wrong key, bug: never retried, never piled up
        log.warning("sweep_failed", sweep=kind, task_id=task_id, error=repr(exc))
        return None
    log.info(f"{kind}_sweep", task_id=task_id, **counts)
    return counts


@app.task(bind=True, name=TASK_STALE_SWEEP, ignore_result=True)
def run_stale_sweep(self: Any) -> dict[str, int] | None:
    """Reset or error files whose worker stopped heartbeating (R11.4)."""
    return _sweep("stale", self.request.id)


@app.task(bind=True, name=TASK_RECONCILE_SWEEP, ignore_result=True)
def run_reconcile_sweep(self: Any) -> dict[str, int] | None:
    """Re-enqueue confirmed files nobody claimed, or error them when attempts are spent (R11.5)."""
    return _sweep("reconcile", self.request.id)
