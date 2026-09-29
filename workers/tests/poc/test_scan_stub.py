"""scan_stub, the POC scan worker's task (task 12.1; design.md §6.1, §6.3; R13)."""
import logging
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from shared.config import get_settings
from shared.constants import QUEUE_SCAN


@pytest.fixture
def scan(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """poc.worker (the scan stub) with a valid minimal environment."""
    monkeypatch.setenv("INTERNAL_API_KEY", "test-key")
    get_settings.cache_clear()
    import poc.worker as module
    yield module
    get_settings.cache_clear()


def test_scan_stub_logs_start_delay_and_finish(scan: Any, monkeypatch: pytest.MonkeyPatch,
                                               caplog: pytest.LogCaptureFixture) -> None:
    """scan_started carries enqueued_at, started_at and the measured delay; scan_finished follows the sleep."""
    slept: list[float] = []
    monkeypatch.setattr(scan.time, "sleep", slept.append)
    enqueued = (datetime.now(timezone.utc) - timedelta(seconds=1.5)).isoformat(timespec="milliseconds")
    with caplog.at_level(logging.INFO, logger="poc.scan_stub"):
        delay = scan.scan_stub.apply(kwargs={"seconds": 1, "enqueued_at": enqueued}).get()
    started = next(r for r in caplog.records if r.getMessage() == "scan_started")
    assert started.fields["enqueued_at"] == enqueued and 1.4 < started.fields["start_delay_s"] < 3
    assert 1.4 < delay < 3 and slept == [1]
    assert [r.getMessage() for r in caplog.records if r.name == "poc.scan_stub"] == ["scan_started", "scan_finished"]


def test_scan_stub_is_registered_on_the_scan_queue() -> None:
    """In a fresh interpreter (as worker-scan starts), poc.worker registers workers.scan.scan_stub and routes it
    to clinsync.scan. Fresh, because Celery caches its route table per process."""
    import os
    import subprocess
    import sys
    code = ("from poc.worker import app; from shared.constants import TASK_SCAN_STUB as t; "
            "print(t in app.tasks, app.amqp.router.route({}, t)['queue'].name)")
    env = {**os.environ, "INTERNAL_API_KEY": os.environ.get("INTERNAL_API_KEY", "test-key")}
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True, env=env).stdout
    assert out.strip().splitlines()[-1] == f"True {QUEUE_SCAN}"
