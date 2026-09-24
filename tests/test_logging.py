"""Unit tests for app.logging: every line is one JSON object with ts, level, event, pid (R15.1)."""
import json
import logging
import os

import pytest

from app.logging import JsonFormatter, get_logger, say_json


@pytest.fixture
def records() -> list[logging.LogRecord]:
    """Capture records emitted through the root logger."""
    captured: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record)

    handler = _Capture()
    root = logging.getLogger()
    root.addHandler(handler)
    old_level = root.level
    root.setLevel(logging.DEBUG)
    yield captured
    root.removeHandler(handler)
    root.setLevel(old_level)


def test_event_logger_emits_fields(records: list[logging.LogRecord]) -> None:
    """log.info(event, **fields) renders event, pid and each field."""
    get_logger("t").info("claim_ok", file_id="f1", attempt=2)
    line = json.loads(JsonFormatter().format(records[-1]))
    assert line["event"] == "claim_ok" and line["level"] == "info"
    assert line["file_id"] == "f1" and line["attempt"] == 2
    assert line["pid"] == os.getpid() and "ts" in line


def test_stdlib_extra_and_exceptions(records: list[logging.LogRecord]) -> None:
    """Plain stdlib calls with extra= and exc_info also render as one JSON line."""
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        logging.getLogger("t").exception("failed %s", "x", extra={"batch_id": "b1"})
    text = JsonFormatter().format(records[-1])
    assert "\n" not in text
    line = json.loads(text)
    assert line["event"] == "failed x" and line["batch_id"] == "b1" and "RuntimeError: boom" in line["exc"]


def test_fields_cannot_override_reserved_keys(records: list[logging.LogRecord]) -> None:
    """A field named like a reserved key does not replace it."""
    get_logger("t").info("real_event", pid=1, level="fake")
    line = json.loads(JsonFormatter().format(records[-1]))
    assert line["event"] == "real_event" and line["pid"] == os.getpid() and line["level"] == "info"


def test_uvicorn_access_record_is_structured() -> None:
    """uvicorn's access record becomes an http_request event with fields."""
    record = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
                               ("127.0.0.1:5000", "GET", "/health", "1.1", 200), None)
    line = json.loads(JsonFormatter().format(record))
    assert line["event"] == "http_request" and line["path"] == "/health" and line["status"] == 200


def test_say_json_writes_one_json_line(capfd: pytest.CaptureFixture[str]) -> None:
    """say_json, used in place of Celery's safe_say, writes a JSON line to stdout."""
    say_json("worker: Warm shutdown (MainProcess)")
    line = json.loads(capfd.readouterr().out.strip())
    assert line["event"] == "worker: Warm shutdown (MainProcess)" and line["pid"] == os.getpid()
