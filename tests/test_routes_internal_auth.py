"""Every /internal route refuses a caller without X-Internal-Key — discovered from the app, not listed by hand."""
import re
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app.main import app


def _walk(routes: list[Any], prefix: str = "") -> Iterator[tuple[str, str]]:
    """Yield (method, path) for every APIRoute, descending into included routers of any shape."""
    for route in routes:
        if isinstance(route, APIRoute):
            for method in sorted(route.methods):
                yield method, prefix + route.path
        elif hasattr(route, "original_router"):            # FastAPI >= 0.14x wraps include_router()
            context = getattr(route, "include_context", None)
            yield from _walk(route.original_router.routes, prefix + (getattr(context, "prefix", "") or ""))
        elif hasattr(route, "routes"):                     # Mount / older FastAPI
            yield from _walk(list(route.routes), prefix + getattr(route, "path", ""))


def internal_routes() -> list[tuple[str, str]]:
    """(method, path template) for every /internal route: route-tree walk plus OpenAPI, so neither can hide one."""
    walked = set(_walk(list(app.routes)))
    documented = {(method.upper(), path) for path, ops in app.openapi()["paths"].items() for method in ops}
    return sorted(r for r in walked | documented if r[1].startswith("/internal"))


def fill(path: str, value: str) -> str:
    """Replace every {param} in the path template with value."""
    return re.sub(r"\{[^}]+\}", value, path)


@pytest.fixture(scope="module")
def client() -> Iterator[TestClient]:
    """A client for the real app."""
    with TestClient(app) as test_client:
        yield test_client


def test_discovery_finds_all_internal_routes() -> None:
    """Guard against a vacuous pass: all eight §6.2 routes are discovered, by the walk and by OpenAPI."""
    assert len(internal_routes()) >= 8
    assert {p for _, p in _walk(list(app.routes)) if p.startswith("/internal")}, "route-tree walk found nothing"


@pytest.mark.parametrize(("method", "path"), internal_routes())
@pytest.mark.parametrize("path_value", [str(uuid.uuid4()), "not-a-uuid"])
def test_internal_route_without_key_is_401(client: TestClient, method: str, path: str, path_value: str) -> None:
    """No key → 401 envelope, even with a malformed path id, no body and no token: callers learn nothing."""
    response = client.request(method, fill(path, path_value))
    assert response.status_code == 401, response.text
    assert response.json()["error"]["code"] == "invalid_internal_key"
