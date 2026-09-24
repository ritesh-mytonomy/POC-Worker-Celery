"""scan_stub and POST /poc/scan-stub (task 12.1; design.md §6.1, §6.3; R13)."""
import logging
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from app import tasks_client
from app.config import get_settings
from app.constants import QUEUE_SCAN, TASK_SCAN_STUB
from app.main import app


@pytest.fixture
def scan(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """workers.scan with a valid minimal environment."""
    monkeypatch.setenv("INTERNAL_API_KEY", "test-key")
    get_settings.cache_clear()
    import workers.scan as module
    yield module
    get_settings.cache_clear()


def test_scan_stub_logs_start_delay_and_finish(scan: Any, monkeypatch: pytest.MonkeyPatch,
                                               caplog: pytest.LogCaptureFixture) -> None:
    """scan_started carries enqueued_at, started_at and the measured delay; scan_finished follows the sleep."""
    slept: list[float] = []
    monkeypatch.setattr(scan.time, "sleep", slept.append)
    enqueued = (datetime.now(timezone.utc) - timedelta(seconds=1.5)).isoformat(timespec="milliseconds")
    with caplog.at_level(logging.INFO, logger="workers.scan"):
        delay = scan.scan_stub.apply(kwargs={"seconds": 1, "enqueued_at": enqueued}).get()
    started = next(r for r in caplog.records if r.getMessage() == "scan_started")
    assert started.fields["enqueued_at"] == enqueued and 1.4 < started.fields["start_delay_s"] < 3
    assert 1.4 < delay < 3 and slept == [1]
    assert [r.getMessage() for r in caplog.records if r.name == "workers.scan"] == ["scan_started", "scan_finished"]


def test_scan_stub_is_registered_on_the_scan_queue(scan: Any) -> None:
    """workers.scan.scan_stub routes to clinsync.scan (the isolated pool)."""
    from workers.celery_app import app as worker_app
    worker_app.loader.import_default_modules()
    assert TASK_SCAN_STUB in worker_app.tasks
    assert worker_app.amqp.router.route({}, TASK_SCAN_STUB)["queue"].name == QUEUE_SCAN


def test_enqueue_scan_stub_publishes_seconds_and_enqueued_at(monkeypatch: pytest.MonkeyPatch) -> None:
    """The message carries only seconds and enqueued_at (design.md §6.3), on clinsync.scan."""
    producer = MagicMock()
    producer.send_task.return_value.id = "t-1"
    monkeypatch.setattr(tasks_client, "get_producer", lambda: producer)
    assert tasks_client.enqueue_scan_stub(1.0, "2026-09-25T10:00:00.000+00:00") == "t-1"
    producer.send_task.assert_called_once_with(TASK_SCAN_STUB, kwargs={"seconds": 1.0,
                                                                      "enqueued_at": "2026-09-25T10:00:00.000+00:00"},
                                               queue=QUEUE_SCAN)


@pytest.fixture
def client() -> Iterator[TestClient]:
    """The real app."""
    with TestClient(app) as test_client:
        yield test_client


def test_route_enqueues_with_a_fresh_utc_timestamp(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """POST /poc/scan-stub → 202 with task_id and enqueued_at (UTC, ms), published once."""
    calls: list[tuple[float, str]] = []
    monkeypatch.setattr(tasks_client, "enqueue_scan_stub", lambda s, e: calls.append((s, e)) or "t-9")
    before = datetime.now(timezone.utc)
    response = client.post("/poc/scan-stub", json={"seconds": 1})
    assert response.status_code == 202
    body = response.json()
    assert body["task_id"] == "t-9" and calls == [(1.0, body["enqueued_at"])]
    assert abs((datetime.fromisoformat(body["enqueued_at"]) - before).total_seconds()) < 2


@pytest.mark.parametrize("seconds", [-1, 61, "x"])
def test_route_rejects_bad_seconds(client: TestClient, seconds: Any) -> None:
    """seconds outside 0..60 → 400."""
    assert client.post("/poc/scan-stub", json={"seconds": seconds}).status_code == 400


def test_route_publish_failure_is_503(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redis down → 503 enqueue_failed."""
    def down(*_: Any) -> None:
        raise ConnectionError("down")

    monkeypatch.setattr(tasks_client, "enqueue_scan_stub", down)
    response = client.post("/poc/scan-stub", json={"seconds": 1})
    assert response.status_code == 503 and response.json()["error"]["code"] == "enqueue_failed"
