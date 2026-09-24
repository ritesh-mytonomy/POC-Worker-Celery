"""Unit tests for app.config: DATABASE_URL defaulting and validate() (R9.4, R9.5)."""
from typing import Any

import pytest

from app.config import Settings


def make(**overrides: Any) -> Settings:
    """Build Settings from POC defaults plus overrides, ignoring .env and the environment."""
    values: dict[str, Any] = {"INTERNAL_API_KEY": "test-key", "DATABASE_URL": None}
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_poc_defaults_are_valid() -> None:
    """The §9 POC values pass validation."""
    make().validate()


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"VISIBILITY_TIMEOUT": 150, "TASK_TIME_LIMIT": 150}, "VISIBILITY_TIMEOUT"),
        ({"VISIBILITY_TIMEOUT": 100}, "VISIBILITY_TIMEOUT"),
        ({"STALE_AFTER_SECONDS": 14, "HEARTBEAT_SECONDS": 5}, "STALE_AFTER_SECONDS"),
        ({"TASK_SOFT_TIME_LIMIT": 150, "TASK_TIME_LIMIT": 150}, "TASK_SOFT_TIME_LIMIT"),
        ({"TASK_SOFT_TIME_LIMIT": 200}, "TASK_SOFT_TIME_LIMIT"),
        ({"INTERNAL_API_KEY": ""}, "INTERNAL_API_KEY"),
        ({"INTERNAL_API_KEY": "   "}, "INTERNAL_API_KEY"),
        ({"INGEST_CONCURRENCY": 0}, "INGEST_CONCURRENCY"),
        ({"MAX_ATTEMPTS": 0}, "MAX_ATTEMPTS"),
    ],
)
def test_invalid_combination_raises(overrides: dict[str, Any], message: str) -> None:
    """Each violated constraint raises and names the offending setting."""
    with pytest.raises(ValueError, match=message):
        make(**overrides).validate()


def test_boundaries_are_valid() -> None:
    """STALE exactly 3 x HEARTBEAT and VISIBILITY one above the hard limit are allowed."""
    make(STALE_AFTER_SECONDS=15, HEARTBEAT_SECONDS=5, VISIBILITY_TIMEOUT=151).validate()


def test_reports_every_violation() -> None:
    """All violations are reported together."""
    with pytest.raises(ValueError) as exc:
        make(VISIBILITY_TIMEOUT=100, STALE_AFTER_SECONDS=1).validate()
    assert "VISIBILITY_TIMEOUT" in str(exc.value) and "STALE_AFTER_SECONDS" in str(exc.value)


def test_database_url_built_from_postgres_values() -> None:
    """DATABASE_URL is built from POSTGRES_* when not set, escaping the password."""
    s = make(POSTGRES_USER="u", POSTGRES_PASSWORD="p@ss/word", POSTGRES_DB="d")
    assert s.DATABASE_URL == "postgresql+psycopg://u:p%40ss%2Fword@postgres:5432/d"


def test_explicit_database_url_wins() -> None:
    """An explicit DATABASE_URL is used as-is."""
    url = "postgresql+psycopg://x:y@db.example:5432/z"
    assert make(DATABASE_URL=url, POSTGRES_USER="ignored").DATABASE_URL == url


def test_csv_lists_parse_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Comma-separated extension lists from the environment parse to lists."""
    monkeypatch.setenv("ALLOWED_TOP_LEVEL_EXT", "docx, .ZIP")
    monkeypatch.setenv("INTERNAL_API_KEY", "k")
    assert Settings(_env_file=None).ALLOWED_TOP_LEVEL_EXT == ["docx", "zip"]
