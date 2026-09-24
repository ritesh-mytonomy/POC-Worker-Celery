"""workers/celery_app.py matches design.md §7 (R9.1–R9.4, R13.1)."""
from collections.abc import Iterator
from typing import Any

import pytest

from app.config import get_settings


@pytest.fixture
def conf(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """The worker app's configuration, with a valid minimal environment."""
    monkeypatch.setenv("INTERNAL_API_KEY", "test-key")
    get_settings.cache_clear()
    import workers.celery_app as module
    yield module.app.conf
    get_settings.cache_clear()


def test_delivery_guarantees(conf: Any) -> None:
    """acks_late, reject_on_worker_lost, prefetch 1, no result backend (R9.1–R9.3)."""
    assert conf.task_acks_late is True and conf.task_reject_on_worker_lost is True
    assert conf.worker_prefetch_multiplier == 1 and conf.task_ignore_result is True


def test_time_limits_and_visibility_timeout_come_from_settings(conf: Any) -> None:
    """Soft/hard limits and the Redis visibility timeout (R9.4) are the configured values."""
    s = get_settings()
    assert (conf.task_soft_time_limit, conf.task_time_limit) == (s.TASK_SOFT_TIME_LIMIT, s.TASK_TIME_LIMIT)
    assert conf.broker_transport_options == {"visibility_timeout": s.VISIBILITY_TIMEOUT}
    assert conf.broker_transport_options["visibility_timeout"] > conf.task_time_limit


def test_routes_send_each_task_family_to_its_queue(conf: Any) -> None:
    """Three queues, one per worker pool (R13.1)."""
    assert conf.task_default_queue == "clinsync.ingest"
    assert conf.task_routes == {"workers.ingest.*": {"queue": "clinsync.ingest"},
                                "workers.scan.*": {"queue": "clinsync.scan"},
                                "workers.sweepers.*": {"queue": "clinsync.maintenance"}}


def test_beat_schedule_is_empty_until_task_11_1(conf: Any) -> None:
    """No sweeper tasks exist yet, so beat must not schedule them (restored in 11.1)."""
    assert conf.beat_schedule == {}
