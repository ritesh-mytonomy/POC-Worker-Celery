"""Tests for app.security.require_internal_key (R4.3)."""
from collections.abc import Iterator

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.config import get_settings
from app.security import require_internal_key

KEY = "s3cret-internal-key"


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """A client for an app with one route guarded like the /internal/* routes (task 4.2 adds the real ones)."""
    monkeypatch.setenv("INTERNAL_API_KEY", KEY)
    get_settings.cache_clear()
    app = FastAPI()

    @app.post("/internal/ping", dependencies=[Depends(require_internal_key)])
    def ping() -> dict[str, str]:
        return {"ok": "yes"}

    with TestClient(app) as test_client:
        yield test_client
    get_settings.cache_clear()


def test_missing_header_returns_401(client: TestClient) -> None:
    """No X-Internal-Key → 401."""
    response = client.post("/internal/ping")
    assert response.status_code == 401 and response.json() == {"detail": "invalid_internal_key"}


@pytest.mark.parametrize("header", ["", "wrong", KEY + "x", KEY[:-1], KEY.upper(), "ключ".encode("utf-8")])
def test_wrong_key_returns_401(client: TestClient, header: str | bytes) -> None:
    """An empty, wrong, longer, shorter, differently cased or non-ASCII (raw bytes) key → 401, never a 500."""
    assert client.post("/internal/ping", headers={"X-Internal-Key": header}).status_code == 401


def test_correct_key_passes(client: TestClient) -> None:
    """The configured key reaches the route."""
    response = client.post("/internal/ping", headers={"X-Internal-Key": KEY})
    assert response.status_code == 200 and response.json() == {"ok": "yes"}
