"""scripts/lib.py log readers skip lines that are not JSON (R15.1, rev 1.4: Celery's own notices may be plain text)."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import lib  # noqa: E402

MIXED = "\n".join([
    json.dumps({"ts": "2026-09-25T06:00:00.000+00:00", "event": "claim_ok", "file_id": "f1", "attempt": 1}),
    "worker: Warm shutdown (MainProcess)",
    "",
    json.dumps({"ts": "2026-09-25T06:00:01.000+00:00", "event": "finished", "file_id": "f1"}),
    "[2026-09-25 06:00:02,000: WARNING/MainProcess] plain Celery line",
    json.dumps({"ts": "2026-09-25T06:00:03.000+00:00", "event": "claim_ok", "file_id": "f2", "attempt": 1}),
])


@pytest.fixture(autouse=True)
def stub_compose(monkeypatch: pytest.MonkeyPatch) -> None:
    """lib.compose returns the mixed stream instead of calling docker."""
    monkeypatch.setattr(lib, "compose", lambda *args, **kwargs: MIXED)


def test_worker_lines_skips_non_json() -> None:
    """Only the three JSON lines come back, in order."""
    assert [l["event"] for l in lib.worker_lines("worker-ingest")] == ["claim_ok", "finished", "claim_ok"]


def test_events_skips_non_json_and_filters_by_file() -> None:
    """events() returns only this file's JSON lines."""
    assert [l["event"] for l in lib.events("worker-ingest", "f1")] == ["claim_ok", "finished"]
