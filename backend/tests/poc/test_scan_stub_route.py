"""POST /poc/scan-stub and its publish, on the API side (task 12.1; design.md §6.1, §6.3; R13)."""
from collections.abc import Iterator
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

import poc.main as producer
from shared.constants import QUEUE_SCAN, TASK_SCAN_STUB
from poc.main import app


def test_enqueue_scan_stub_publishes_seconds_and_enqueued_at(monkeypatch: pytest.MonkeyPatch) -> None:
    """The message carries only seconds and enqueued_at (design.md §6.3), on clinsync.scan."""
    fake = MagicMock()
    fake.send_task.return_value.id = "t-1"
    monkeypatch.setattr(producer.task_producer, "get_producer", lambda: fake)
    assert producer.enqueue_scan_stub(1.0, "2026-09-25T10:00:00.000+00:00") == "t-1"
    fake.send_task.assert_called_once_with(TASK_SCAN_STUB, kwargs={"seconds": 1.0,
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
    monkeypatch.setattr(producer, "enqueue_scan_stub", lambda s, e: calls.append((s, e)) or "t-9")
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

    monkeypatch.setattr(producer, "enqueue_scan_stub", down)
    response = client.post("/poc/scan-stub", json={"seconds": 1})
    assert response.status_code == 503 and response.json()["error"]["code"] == "enqueue_failed"
