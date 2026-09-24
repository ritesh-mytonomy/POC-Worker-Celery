"""Celery app — minimal stub for Checkpoint A; replaced by task 6.1 (design.md §7)."""
import os

from celery import Celery

app = Celery("clinsync", broker=os.environ["REDIS_URL"])
app.conf.broker_connection_retry_on_startup = True
