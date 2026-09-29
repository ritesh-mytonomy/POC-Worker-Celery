"""Shared fixtures for the worker tests: the generated test files (tests/fixtures/make_fixtures.py)."""
import sys
from pathlib import Path

import pytest


@pytest.fixture(scope="session")
def fixtures_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Every design.md §10.1 fixture, built once per test session into a temporary directory."""
    sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures"))
    import make_fixtures

    out = tmp_path_factory.mktemp("fixtures")
    make_fixtures.build(out)
    return out
