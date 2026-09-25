"""The POC's Celery entry point for worker-scan: the production app plus the POC-only scan stub.

worker-scan runs `celery -A poc.celery_app worker -Q clinsync.scan`; worker-ingest and worker-maint keep
`workers.celery_app`.
"""
from app.constants import QUEUE_SCAN
from workers.celery_app import app

app.conf.task_routes = {**app.conf.task_routes, "workers.scan.*": {"queue": QUEUE_SCAN}}

import poc.scan_stub  # noqa: E402,F401  — registers workers.scan.scan_stub on the app above

__all__ = ["app"]
