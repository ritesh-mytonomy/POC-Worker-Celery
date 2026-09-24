"""Guards the safe_say patch in workers/celery_app.py, which relies on Celery internals."""
import inspect

import celery
import celery.apps.worker
import pytest

from app.config import get_settings
from app.logging import say_json


@pytest.fixture
def celery_app_module(monkeypatch: pytest.MonkeyPatch) -> object:
    """Import workers.celery_app with a valid minimal environment."""
    monkeypatch.setenv("INTERNAL_API_KEY", "test-key")
    get_settings.cache_clear()
    import workers.celery_app as module
    yield module
    get_settings.cache_clear()


def test_safe_say_is_patched(celery_app_module: object) -> None:
    """Importing the Celery app replaces Celery's plain-text safe_say with say_json."""
    assert celery.apps.worker.safe_say is say_json


def test_celery_still_uses_safe_say_for_shutdown() -> None:
    """The shutdown notice is still emitted via safe_say, so the patch still takes effect."""
    assert "safe_say(f'worker: {how} shutdown" in inspect.getsource(celery.apps.worker)


def test_celery_version_is_the_pinned_one() -> None:
    """The patch was verified against this exact Celery version (requirements.txt)."""
    assert celery.__version__ == "5.6.3"
