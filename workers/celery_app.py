"""Celery app — minimal stub for Checkpoint A; replaced by task 6.1 (design.md §7)."""
from celery import Celery

from app.config import get_settings

settings = get_settings()

app = Celery("clinsync", broker=settings.REDIS_URL)
app.conf.broker_connection_retry_on_startup = True
settings.validate()                      # R9.5 — refuse to start on a bad combination
