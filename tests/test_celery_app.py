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


def test_beat_schedule_runs_both_sweeps_every_interval_with_expiry(conf: Any) -> None:
    """Both sweeps every SWEEP_INTERVAL_SECONDS (design.md §7); each message expires after one interval (11.1)."""
    interval = get_settings().SWEEP_INTERVAL_SECONDS
    assert conf.beat_schedule == {
        "stale-sweep": {"task": "workers.sweepers.run_stale_sweep", "schedule": interval,
                        "options": {"expires": interval}},
        "reconcile-sweep": {"task": "workers.sweepers.run_reconcile_sweep", "schedule": interval,
                            "options": {"expires": interval}},
    }


def test_sweep_tasks_are_registered_and_routed_to_maintenance(conf: Any) -> None:
    """include= registers workers.sweepers; both route to clinsync.maintenance.

    Imports the include= modules exactly as a starting worker does, so the test does not depend on another test
    having imported workers.sweepers first.
    """
    from workers.celery_app import app
    app.loader.import_default_modules()
    for name in ("workers.sweepers.run_stale_sweep", "workers.sweepers.run_reconcile_sweep"):
        assert name in app.tasks
        assert app.amqp.router.route({}, name)["queue"].name == "clinsync.maintenance"
