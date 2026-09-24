"""JSON logging for the API and workers: one object per line with ts, level, event, pid, plus extras (R15.1)."""
import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any

# Attributes every LogRecord has; anything else on a record came from `extra=` and is emitted.
_STANDARD_ATTRS = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime", "taskName"}
# Attributes that libraries attach but that duplicate the message (uvicorn's coloured copy).
_DROPPED_ATTRS = {"color_message"}
_RESERVED_KEYS = ("ts", "level", "event", "logger", "pid")


class JsonFormatter(logging.Formatter):
    """Render a LogRecord as a single JSON line."""

    def format(self, record: logging.LogRecord) -> str:
        """Return the record as a JSON object string."""
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "event": record.getMessage(),
            "logger": record.name,
            "pid": record.process,
        }
        if record.name == "uvicorn.access" and isinstance(record.args, tuple) and len(record.args) == 5:
            client, method, path, http_version, status = record.args
            payload["event"] = "http_request"
            payload.update(client=client, method=method, path=path, http_version=http_version, status=status)
        fields: dict[str, Any] = dict(getattr(record, "fields", {}))
        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRS and key not in _DROPPED_ATTRS and key != "fields":
                fields[key] = value
        for key, value in fields.items():
            if key not in _RESERVED_KEYS:
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)
        return json.dumps(payload, default=str)


class EventLogger:
    """Logger taking an event name plus keyword fields: `log.info("claim_ok", file_id=...)`."""

    def __init__(self, name: str) -> None:
        """Wrap the stdlib logger called `name`."""
        self._logger = logging.getLogger(name)

    def _log(self, level: int, event: str, exc_info: bool, fields: dict[str, Any]) -> None:
        """Emit `event` at `level` with `fields` attached."""
        if self._logger.isEnabledFor(level):
            self._logger.log(level, event, exc_info=exc_info, extra={"fields": fields}, stacklevel=3)

    def debug(self, event: str, **fields: Any) -> None:
        """Log at DEBUG."""
        self._log(logging.DEBUG, event, False, fields)

    def info(self, event: str, **fields: Any) -> None:
        """Log at INFO."""
        self._log(logging.INFO, event, False, fields)

    def warning(self, event: str, **fields: Any) -> None:
        """Log at WARNING."""
        self._log(logging.WARNING, event, False, fields)

    def error(self, event: str, **fields: Any) -> None:
        """Log at ERROR."""
        self._log(logging.ERROR, event, False, fields)

    def critical(self, event: str, **fields: Any) -> None:
        """Log at CRITICAL."""
        self._log(logging.CRITICAL, event, False, fields)

    def exception(self, event: str, **fields: Any) -> None:
        """Log at ERROR with the current exception's traceback."""
        self._log(logging.ERROR, event, True, fields)


def get_logger(name: str) -> EventLogger:
    """Return an EventLogger for `name`."""
    return EventLogger(name)


class _DropHealthChecks(logging.Filter):
    """Drop uvicorn access records for GET /health (the container healthcheck polls it)."""

    def filter(self, record: logging.LogRecord) -> bool:
        """Return False for a /health access record."""
        args = record.args
        return not (isinstance(args, tuple) and len(args) == 5 and str(args[2]).split("?")[0] == "/health")


def say_json(msg: str, _stream: Any = None, level: str = "WARNING", name: str = "celery.apps.worker") -> None:
    """Write `msg` as one JSON line straight to stdout, bypassing logging locks (safe in signal handlers)."""
    record = logging.makeLogRecord({"msg": msg, "levelname": level, "levelno": logging.getLevelName(level),
                                    "name": name})
    sys.__stdout__.write(JsonFormatter().format(record) + "\n")
    sys.__stdout__.flush()


def configure_logging(level: str = "INFO") -> None:
    """Send all logging, including uvicorn, celery and warnings, to stdout as JSON."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
    # uvicorn installs its own plain-text handlers before importing the app; route them to root.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers = []
        logger.propagate = True
    access = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, _DropHealthChecks) for f in access.filters):
        access.addFilter(_DropHealthChecks())
    logging.captureWarnings(True)
